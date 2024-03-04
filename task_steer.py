import sys
sys.path.append("../../pyvene/")

import torch
import argparse
from tqdm import tqdm, trange
from transformers import (
    AutoConfig,
    AutoTokenizer, 
    AutoModelForCausalLM, 
    AutoModelForSequenceClassification,
    DataCollatorForSeq2Seq,
    DataCollatorWithPadding,
    get_linear_schedule_with_warmup,
    set_seed
)
import wandb
import datetime
import json
import math
from torch.nn import CrossEntropyLoss
from torch.utils.data import DataLoader

import pyvene as pv
from data import load_task
from trainer import (
    ReftTrainer,
    ReftTrainerForSequenceClassification,
    TrainingArguments,
    compute_metrics,
)
from interventions import *

device = "cuda" if torch.cuda.is_available() else "cpu"
classification_tasks = {"glue"}
residual_stream_component_mapping = {
    "robertaformaskedlm": "roberta.encoder.layer[%s].output"
}

def main():
    """
    Generic Representation Finetuning.
    """

    parser = argparse.ArgumentParser(description="A simple script that takes different arguments.")
    
    parser.add_argument('-task', '--task', type=str, default=None)
    parser.add_argument('-train_dataset', '--train_dataset', type=str, default=None)
    parser.add_argument('-eval_dataset', '--eval_dataset', type=str, default=None)
    parser.add_argument('-model', '--model', type=str, help='yahma/llama-7b-hf', default='yahma/llama-7b-hf')
    parser.add_argument('-seed', '--seed', type=int, help='42', default=42)
    parser.add_argument('-l', '--layers', type=str, help='2;10;18;26', default='2;10;18;26')
    parser.add_argument('-r', '--rank', type=int, help=8, default=8)
    parser.add_argument('-p', '--position', type=str, help='last', default='last')
    parser.add_argument('-e', '--epochs', type=int, help='1', default=1)
    parser.add_argument('-is_wandb', '--is_wandb', action='store_true')
    parser.add_argument('-save_model', '--save_model', type=bool, default=False)
    parser.add_argument('-max_n_train_example', '--max_n_train_example', type=int, default=None)
    parser.add_argument('-max_n_eval_example', '--max_n_eval_example', type=int, default=None)
    parser.add_argument(
        '-type', '--intervention_type', type=str, 
        help='LearnedSourceLowRankRotatedSpaceIntervention', default="LearnedSourceLowRankRotatedSpaceIntervention")
    parser.add_argument('-gradient_accumulation_steps', '--gradient_accumulation_steps', type=int, default=4)
    parser.add_argument('-batch_size', '--batch_size', type=int, default=4)
    parser.add_argument('-eval_batch_size', '--eval_batch_size', type=int, default=4)
    parser.add_argument('-output_dir', '--output_dir', type=str, default="./official_results")
    parser.add_argument('-lr', '--lr', type=float, default=5e-3)
    parser.add_argument('-wd', '--weight_decay', type=float, default=0.00)
    parser.add_argument('-dropout', '--dropout', type=float, default=0.00)
    parser.add_argument('-act_fn', '--act_fn', type=str, default=None)
    parser.add_argument('-test_split', '--test_split', type=str, default="validation")
    
    args = parser.parse_args()

    model = args.model
    layers = args.layers
    rank = args.rank
    position = args.position
    epochs = args.epochs
    seed = args.seed
    intervention_type = args.intervention_type
    max_n_train_example = args.max_n_train_example
    max_n_eval_example = args.max_n_eval_example
    is_wandb = args.is_wandb
    gradient_accumulation_steps = args.gradient_accumulation_steps
    batch_size = args.batch_size
    output_dir = args.output_dir
    task = args.task
    lr = args.lr
    train_dataset = args.train_dataset
    eval_dataset = args.eval_dataset
    save_model = args.save_model
    eval_batch_size = args.eval_batch_size
    weight_decay = args.weight_decay
    dtype = torch.bfloat16 if device == "cuda" else torch.float32
    dropout = args.dropout
    test_split = args.test_split
    
    assert task in {
        "commonsense", "math", "alpaca", "instruct", "ultrafeedback", "glue"
    }
    
    # store/log run details
    print(
        f"task: {task}, model: {model}, intervention_type: {intervention_type}, "
        f"layers: {layers}, rank: {rank}, "
        f"position: {position}, epoch: {epochs}"
    )

    # everything is guarded by a single seed
    set_seed(seed)

    model_name = model
    model_str = model.split("/")[-1]
    train_dataset_str = train_dataset
    now = datetime.datetime.now().strftime("%Y%m%d%H%M%S%f")
    if train_dataset is not None:
        run_name = f"{model_str}.{task}.{train_dataset_str}.{test_split}.{now}"
    else:
        run_name = f"{model_str}.{task}.{now}"

    # which layers to intervene on
    user_give_all_layers = False
    if layers != "all":
        if "+" in layers:
            parsed_layers = []
            for l in layers.split("+"):
                for ll in l.split(";"):
                    parsed_layers += [int(ll)]
            user_give_all_layers = True
            layers = parsed_layers
        else:
            layers = [int(l) for l in layers.split(";")]
    else:
        layers = [l for l in range(config.num_hidden_layers)]
    assert position in {"first", "last", "first+last"}
    if position in {"first+last"}:
        if user_give_all_layers:
            pass
        else:
            layers += layers

    # load model based on task type.
    if task in classification_tasks:
        config = AutoConfig.from_pretrained(
            model, num_labels=num_labels,
            finetuning_task=train_dataset_str,
        )
        # full precision loading since usually for small models
        model = AutoModelForSequenceClassification.from_pretrained(
            model,
            config=config, # just providing the label
            torch_dtype=dtype
        )
    else:
        # load llama model
        config, _, model = pv.create_llama(model, dtype=dtype)

    # load tokenizer
    tokenizer = AutoTokenizer.from_pretrained(model_name)
    tokenizer.padding_side = "right" # we will use right padding for training with teacher-forcing
    tokenizer.pad_token = tokenizer.unk_token
    _ = model.to(device)

    # load dataset splits
    train_dataset, eval_datasets, trigger_tokens, num_labels = load_task(
        task, tokenizer, max_n_train_example, max_n_eval_example, train_dataset,
        eval_dataset, test_split, seed, eval_batch_size, position, layers)
    print("loaded", len(train_dataset), len(eval_datasets), num_labels)

    # post-processing the inputs
    if intervention_type == "LearnedSourceLowRankRotatedSpaceIntervention":
        intervention_type = LearnedSourceLowRankRotatedSpaceIntervention
    elif intervention_type == "ConditionedSourceLowRankRotatedSpaceIntervention":
        intervention_type = ConditionedSourceLowRankRotatedSpaceIntervention
    elif intervention_type == "ConditionedSourceLowRankIntervention":
        intervention_type = ConditionedSourceLowRankIntervention
    
    # select collator based on the type
    if task in classification_tasks:
        data_collator = DataCollatorWithPadding(
            tokenizer=tokenizer,
            padding="longest"
        )
    else:
        data_collator = DataCollatorForSeq2Seq(
            tokenizer=tokenizer,
            model=model,
            label_pad_token_id=-100,
            padding="longest"
        )

    # intervention config based on model type
    model_arch = model.config.architectures[0].lower()
    if model_arch in residual_stream_component_mapping:
        config = pv.IntervenableConfig([{
            "component": residual_stream_component_mapping[model_arch] % l,
            "intervention": intervention_type(
                embed_dim=config.hidden_size, low_rank_dimension=rank,
                dropout=dropout, dtype=dtype
            )
        } for l in layers])
    else:
        config = pv.IntervenableConfig([{
            "layer": l, "component": "block_output",
            "low_rank_dimension": rank,
            "intervention": intervention_type(
                embed_dim=config.hidden_size, low_rank_dimension=rank,
                dropout=dropout, dtype=dtype
            )
        } for l in layers])

    reft_model = pv.IntervenableModel(config, model)
    reft_model.set_device(device)
    reft_model.disable_model_gradients()

    reft_model.model.train()  # train enables dropout but no grads
    n_params = reft_model.count_parameters()

    # start wandb logging
    if is_wandb:
        run = wandb.init(
            project=f"Steer_LM_{task}", 
            entity="reft",
            name="zen_debug."+run_name,
        )
        run.summary.update(vars(args))
        wandb.log(
            {"train/n_params": n_params})

    #####################################################
    # 
    # Test: Start of training loop.
    #
    #####################################################
    # train_dataloader = DataLoader(
    #     train_dataset, shuffle=True, batch_size=batch_size, collate_fn=data_collator)
    # t_total = int(len(train_dataloader) * epochs) // gradient_accumulation_steps

    # optimizer = torch.optim.Adam(
    #     reft_model.get_trainable_parameters(), lr=lr
    # )
    # scheduler = get_linear_schedule_with_warmup(
    #     optimizer, 
    #     num_warmup_steps=int(t_total*0.1), 
    #     num_training_steps=t_total
    # )

    # train_iterator = trange(0, int(epochs), desc="Epoch")
    # global_step = 0
    # for epoch in train_iterator:
    #     total_step = 0
    #     epoch_iterator = tqdm(
    #         train_dataloader, desc=f"Epoch: {epoch}", position=0, leave=True
    #     )
    #     for step, inputs in enumerate(epoch_iterator):
    #         for k, v in inputs.items():
    #             if v is not None and isinstance(v, torch.Tensor):
    #                 inputs[k] = v.to(device)
            
    #         _, cf_outputs = reft_model(
    #             {
    #                 "input_ids": inputs["input_ids"],
    #                 "attention_mask": inputs["attention_mask"]
    #             },
    #             unit_locations={"sources->base": (
    #                 None,
    #                 inputs["intervention_locations"].permute(1, 0, 2).tolist()
    #             )}
    #         )

    #         # lm loss on counterfactual labels
    #         lm_logits = cf_outputs.logits
    #         labels = inputs["labels"]
    #         shift_logits = lm_logits[..., :-1, :].contiguous()
    #         shift_labels = labels[..., 1:].contiguous()
    #         # Flatten the tokens
    #         loss_fct = CrossEntropyLoss()
    #         loss = loss_fct(shift_logits.view(-1, shift_logits.size(-1)), shift_labels.view(-1))
    #         loss_str = round(loss.item(), 2)
    #         epoch_iterator.set_postfix({"loss": loss_str})
    #         if gradient_accumulation_steps > 1:
    #             loss = loss / gradient_accumulation_steps
    #         loss.backward()
    #         if (total_step % gradient_accumulation_steps == 0) or (total_step == len(train_dataloader) - 1):
    #             if not (gradient_accumulation_steps > 1 and total_step == 0):
    #                 if is_wandb:
    #                     wandb.log({
    #                         "train/loss": loss_str,
    #                         "train/learning_rate": scheduler.get_last_lr()[0]}, step=global_step)
    #                 optimizer.step()
    #                 scheduler.step()
    #                 # if do model.zero_grad() make sure model implements this.
    #                 optimizer.zero_grad()
    #                 global_step += 1
    #         total_step += 1
    #####################################################
    # 
    # Test: End of training loop.
    #
    #####################################################

    t_total = int(math.ceil(len(train_dataset)/(batch_size*gradient_accumulation_steps)) * epochs)
    optimizer = torch.optim.Adam(
        reft_model.get_trainable_parameters(), lr=lr
    )
    scheduler = get_linear_schedule_with_warmup(
        optimizer, 
        num_warmup_steps=int(t_total*0.1), 
        num_training_steps=t_total
    )

    # # training args
    training_args = TrainingArguments(
        output_dir=f"{output_dir}/{run_name}",
        run_name=run_name,
        num_train_epochs=epochs,
        per_device_train_batch_size=batch_size,
        per_device_eval_batch_size=eval_batch_size,
        gradient_accumulation_steps=gradient_accumulation_steps,
        evaluation_strategy="no",
        save_strategy="no",
        logging_strategy="steps",
        save_total_limit=1,
        logging_steps=1,
        learning_rate=lr,
        warmup_ratio=0.1,
        report_to="wandb" if is_wandb else "none",
        use_cpu=False if device == "cuda" else True,
    )

    # make trainer
    trainer_class = ReftTrainerForSequenceClassification if task in classification_tasks else ReftTrainer
    trainer = trainer_class(
        model=reft_model,
        tokenizer=tokenizer,
        args=training_args,
        data_collator=data_collator,
        train_dataset=train_dataset,
        eval_dataset=None,
        compute_metrics=None,
        optimizers=(optimizer, scheduler),
    )
    trainer.train()

    # dump config
    args_dict = vars(args)
    args_dict["n_params"] = n_params
    json_file_name = f"{output_dir}/{run_name}/args.json"
    with open(json_file_name, 'w') as json_file:
        json.dump(args_dict, json_file, indent=4)

    # ensure everything is in eval mode
    reft_model.model.eval()
    for k,v in reft_model.interventions.items():
        _ = v[0].eval()

    print({"n_params": n_params})
    # do eval
    eval_results = {}
    for dataset_name in eval_datasets:
        # split evalset into chunks
        eval_dataset, data_items = eval_datasets[dataset_name]
        generations, stats = compute_metrics(
            task, dataset_name, reft_model, tokenizer, eval_dataset, data_items,
            trigger_tokens, run_name, eval_batch_size, 
            data_collator if task in classification_tasks else None
        )

        # log
        eval_results.update(stats)
        if is_wandb:
            wandb.log(stats)
        generations = stats if generations is None else generations
        result_json_file_name = f"{output_dir}/{run_name}/{dataset_name}_outputs.json"
        with open(result_json_file_name, 'w') as json_file:
            json.dump(generations, json_file, indent=4)

    # log final eval stats
    result_json_file_name = f"{output_dir}/{run_name}/eval_results.json"
    eval_results["n_params"] = n_params
    with open(result_json_file_name, 'w') as json_file:
        json.dump(eval_results, json_file, indent=4)
        
if __name__ == "__main__":
    main()
