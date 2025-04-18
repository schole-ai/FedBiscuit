import logging
import torch
import random
from torch.utils.data import DataLoader
import os
import json
from tqdm import tqdm
import argparse
import numpy as np
import copy

from federatedscope.core.configs.config import global_cfg
from federatedscope.core.cmd_args import parse_args, parse_client_cfg
from federatedscope.core.auxiliaries.utils import setup_seed
from federatedscope.core.auxiliaries.logging import update_logger
from federatedscope.core.data.utils import download_url
from federatedscope.llm.model.model_builder import get_llm
from federatedscope.llm.dataloader.dataloader import load_jsonl, \
    LLMDataCollator, get_tokenizer
from federatedscope.llm.misc.fschat import FSChatBot
from federatedscope.llm.dataloader.reddit_tldr import TLDR_PROMPT_DICT
from federatedscope.llm.dataset.llm_dataset import DefaultToken, \
    LLMDataset
# from federatedscope.llm.eval.eval_for_tldr.auto_j_vllm import evaluation

import sys

sys.setrecursionlimit(100000)

logger = logging.getLogger(__name__)


def get_input_data(list_data_dict, w=5):
    for left in tqdm(range(0, len(list_data_dict), w)):
        yield list_data_dict[left:left + w]


@torch.no_grad()
def _generate_best_of_n_dataset(gen_cfg, n=16):
    # load your finetuned model (saved as xxx.ckpt)
    #    in yaml file federate.save_to
    fschatbot = FSChatBot(gen_cfg)

    # create the dataset
    fp = os.path.join(gen_cfg.data.root, 'reddit-tldr_test.jsonl')
    if not os.path.exists(fp):
        download_url(
            'https://openaipublic.blob.core.windows.net/'
            'summarize-from-feedback/datasets/'
            'tldr_3_filtered/test.jsonl', gen_cfg.data.root)
        os.rename(os.path.join(gen_cfg.data.root, 'test.jsonl'), fp)

    list_data_dict = load_jsonl(fp,
                                subreddit='subreddit',
                                title='title',
                                post='post',
                                summary='summary')

    prompt = TLDR_PROMPT_DICT["summary"]

    results_display = os.path.join(gen_cfg.outdir,
                                   f'{fschatbot.curpfx}_summarization.txt')
    results_display = open(results_display, 'w')

    for input_data in get_input_data(list_data_dict):
        input_texts = [prompt.format_map(data) for data in input_data]
        generate_kwargs = dict(
            top_p=1.0,
            temperature=0.7,
            do_sample=True,
            max_new_tokens=80,
            num_return_sequences=n,
        )
        model_completions = fschatbot.generate(input_texts, generate_kwargs)

        for i, sample in enumerate(input_data):
            results_display.write(f'Post:\n{sample["post"]}\n\n'
                                  f'Human summary:\n{sample["summary"]}\n\n')
            summaries = []
            for j, completion in enumerate(model_completions[i]):
                results_display.write(
                    f'Model-generated summary {j}:\n{completion}\n\n')
                summaries.append(completion)
            sample['summaries'] = summaries

            results_display.write('==========================\n\n')
            results_display.flush()

    return list_data_dict


@torch.no_grad()
def best_of_n_dataset(init_cfg, gen_cfg, n=16, load_eval_version=False):
    _, model_name = gen_cfg.model.type.split("@")[0].split('/', 1)
    gen_fp = os.path.join(init_cfg.data.root, 'reddit-tldr-comparison',
                          f'reddit-tldr_test_{n}-gen_{model_name}.json')
    if load_eval_version and os.path.exists(gen_fp + '_autoj_eval.json'):
        list_data_dict_eval = json.load(open(gen_fp + '_autoj_eval.json', 'r'))
        return list_data_dict_eval

    if os.path.exists(gen_fp):
        # load the dataset
        list_data_dict = json.load(open(gen_fp, "r"))
    else:
        # create the dataset
        list_data_dict = _generate_best_of_n_dataset(gen_cfg, n)
        json.dump(list_data_dict, open(gen_fp, "w"))

    return list_data_dict


def best_of_n_dataset_eval(init_cfg, gen_cfg, n=16):
    from auto_j_vllm import auto_j_eval_rating
    _, model_name = gen_cfg.model.type.split("@")[0].split('/', 1)
    file_path = os.path.join(init_cfg.data.root, 'reddit-tldr-comparison',
                             f'reddit-tldr_test_{n}-gen_{model_name}.json')
    if os.path.exists(file_path):
        # load the dataset
        list_data_dict = json.load(open(file_path, "r"))
    else:
        raise FileNotFoundError('Create the best_of_n dataset first')

    dataset = []
    for data in list_data_dict:
        for idx, summary in enumerate(data['summaries']):
            query = ("Summarize the following post\n\n"
                     "Title: {title}\n\n"
                     "Post: {post}").format_map(data)
            record = {
                'subreddit': data['subreddit'],
                'title': data['title'],
                'post': data['post'],
                'response': summary,
                'choice': idx,
                'query': query
            }
            dataset.append(record)

    with open(f'{file_path}_comments.txt', 'w') as comments_f, \
            open(f'{file_path}_ratings.txt', 'w') as ratings_f:
        auto_j_comments, auto_j_ratings = auto_j_eval_rating(dataset)
        comments_f.write(str(auto_j_comments))
        ratings_f.write(str(auto_j_ratings))

    eval_results = {data["title"]: data for data in list_data_dict}
    for sample, comment, rating in zip(dataset, auto_j_comments,
                                       auto_j_ratings):
        title = sample["title"]
        if "autoj_eval_results" not in eval_results[title]:
            eval_results[title]["autoj_eval_results"] = dict()
        idx = sample["choice"]
        eval_results[title]["autoj_eval_results"][idx] = {
            "comment": comment,
            "rating": rating
        }
    json.dump(list(eval_results.values()),
              open(file_path + '_autoj_eval.json', 'w'))


@torch.no_grad()
def cal_acc(logits, labels, choices):
    shift_logits = logits[..., :-1, :].contiguous()
    shift_labels = labels[..., 1:].contiguous()

    new_labels = torch.full_like(shift_labels, DefaultToken.IGNORE_INDEX.value)
    for idx, choice in enumerate(choices):
        new_labels[shift_labels == choice] = idx

    new_labels = new_labels.view(-1)
    new_logits = shift_logits[..., choices].view(-1, len(choices))
    new_logits = new_logits[(new_labels != DefaultToken.IGNORE_INDEX.value), :]
    # print(new_logits)
    new_labels = new_labels[(new_labels != DefaultToken.IGNORE_INDEX.value)]
    _, predicted = new_logits.max(1)

    return new_labels, new_logits, predicted, predicted.eq(
        new_labels).sum().item()


@torch.no_grad()
def best_of_n(model, dataset, tokenizer, n=16):
    prompt = TLDR_PROMPT_DICT["summary_cmp"]

    choices = [tokenizer(f': {c}')['input_ids'][-1] for c in ['A', 'B']]
    # Correct idx is 0 means no changed, 1 means changed to the new index
    last_better_idx = np.array([0] * len(dataset))
    for i in range(1, n):
        logger.info(f'===== This is {i}-th evaluation =====')
        eval_dataset = []

        for better_idx, sample in zip(last_better_idx, dataset):
            sample['output_A'] = sample['summaries'][better_idx]
            if sample['output_A'].startswith(" ") is False:
                sample['output_A'] = " " + sample['output_A']
            sample['output_B'] = sample['summaries'][i]
            if sample['output_B'].startswith(" ") is False:
                sample['output_B'] = " " + sample['output_B']
            sample['choice'] = random.choice([" A", " B"])
            eval_dataset.append({
                'subreddit': sample['subreddit'],
                'title': sample['title'],
                'post': sample['post'],
                'output_A': sample['output_A'],
                'output_B': sample['output_B'],
                'choice': sample['choice']
            })

        test_dataset = LLMDataset(eval_dataset,
                                  tokenizer,
                                  prompt_input=prompt,
                                  prompt_no_input=prompt,
                                  output_tag='choice')
        dataloader = DataLoader(
            dataset=test_dataset,
            batch_size=n,
            shuffle=False,
            collate_fn=LLMDataCollator(tokenizer=tokenizer))

        predicted_indices = []
        for idx, data_batch in enumerate(tqdm(dataloader)):
            input_ids = data_batch["input_ids"].to('cuda:0')
            labels = data_batch["labels"].to('cuda:0')
            attention_mask = data_batch["attention_mask"].to('cuda:0')
            outputs = model(input_ids=input_ids, attention_mask=attention_mask)
            _, new_logits, predicted, _ = cal_acc(outputs.logits, labels,
                                                  choices)
            predicted_indices += predicted.tolist()

        predicted_indices = np.array(predicted_indices)
        last_better_idx[predicted_indices == 1] = i

    # print the final results
    return last_better_idx


@torch.no_grad()
def best_of_n_by_reward(model, dataset, tokenizer, n=16):
    prompt = TLDR_PROMPT_DICT["summary"]

    best_idx = np.array([0] * len(dataset))
    eval_dataset, eval_rating = [], []

    # Load the dataset
    for idx, sample in enumerate(tqdm(dataset)):
        for i in range(n):
            eval_dataset.append({
                'subreddit': sample['subreddit'],
                'title': sample['title'],
                'post': sample['post'],
                'summary': sample['summaries'][i]
            })

    test_dataset = LLMDataset(eval_dataset,
                              tokenizer,
                              prompt_input=prompt,
                              prompt_no_input=prompt,
                              output_tag='summary')
    dataloader = DataLoader(dataset=test_dataset,
                            batch_size=25,
                            shuffle=False,
                            collate_fn=LLMDataCollator(tokenizer=tokenizer))

    for data_batch in tqdm(dataloader):
        input_ids = data_batch["input_ids"].to('cuda:0')
        attention_mask = data_batch["attention_mask"].to('cuda:0')
        outputs = model(input_ids=input_ids, attention_mask=attention_mask)
        eval_rating += outputs.logits.tolist()

    eval_rating = np.array(eval_rating).reshape((-1, n))
    best_idx = np.argmax(eval_rating, axis=1)
    print(best_idx)

    return best_idx


@torch.no_grad()
def best_of_n_local(model,
                    dataset,
                    tokenizer,
                    n=16,
                    num_clients=1,
                    output_dir=None,
                    print_client_result=True):
    clients_best_idx = []
    for client_id in range(num_clients):
        logger.info(f'============ Client {client_id+1} ============')
        model.set_active_adapter(f'Client_{client_id+1}')
        clients_best_idx.append(
            best_of_n(model, dataset, tokenizer, n, output_dir))
        if print_client_result:
            path = os.path.join(output_dir,
                                f'test_results_client_{client_id+1}.txt')
            print_results(open(path, 'w'), dataset, clients_best_idx[-1])
            # # evaluate best-of-n selection using auto_j
            # evaluation(path)

    # Choose the indices with most votes
    array = np.array(clients_best_idx).T
    majority_votes_idx = [
        np.bincount(array[i]).argmax() for i in range(len(array))
    ]

    return clients_best_idx, majority_votes_idx


@torch.no_grad()
def best_of_n_multilora(model, dataset, tokenizer, n=16):
    prompt = TLDR_PROMPT_DICT["summary_cmp"]

    choices = [tokenizer(f': {c}')['input_ids'][-1] for c in ['A', 'B']]
    # Correct idx is 0 means no changed, 1 means changed to the new index
    last_better_idx = np.array([0] * len(dataset))
    for i in range(1, n):
        logger.info(f'===== This is {i}-th evaluation =====')
        eval_dataset = []

        for better_idx, sample in zip(last_better_idx, dataset):
            sample['output_A'] = sample['summaries'][better_idx]
            if sample['output_A'].startswith(" ") is False:
                sample['output_A'] = " " + sample['output_A']
            sample['output_B'] = sample['summaries'][i]
            if sample['output_B'].startswith(" ") is False:
                sample['output_B'] = " " + sample['output_B']
            sample['choice'] = random.choice([" A", " B"])
            eval_dataset.append({
                'subreddit': sample['subreddit'],
                'title': sample['title'],
                'post': sample['post'],
                'output_A': sample['output_A'],
                'output_B': sample['output_B'],
                'choice': sample['choice']
            })

        test_dataset = LLMDataset(eval_dataset,
                                  tokenizer,
                                  prompt_input=prompt,
                                  prompt_no_input=prompt,
                                  output_tag='choice')
        dataloader = DataLoader(
            dataset=test_dataset,
            batch_size=n,
            shuffle=False,
            collate_fn=LLMDataCollator(tokenizer=tokenizer))

        predicted_indices = []

        for idx, data_batch in enumerate(tqdm(dataloader)):
            input_ids = data_batch["input_ids"].to('cuda:0')
            labels = data_batch["labels"].to('cuda:0')
            attention_mask = data_batch["attention_mask"].to('cuda:0')
            collective_choices = []
            for name in model.adapter_names:
                if name == 'default':
                    continue
                model.set_active_adapter(name)
                model.eval()
                outputs = model(input_ids=input_ids,
                                attention_mask=attention_mask)
                _, new_logits, predicted, _ = cal_acc(outputs.logits, labels,
                                                      choices)
                collective_choices.append(predicted.tolist())
            # finalize the output chosen by most adapters
            array = np.array(collective_choices).T
            predicted_indices += [
                np.bincount(array[i]).argmax() for i in range(len(array))
            ]

        predicted_indices = np.array(predicted_indices)
        last_better_idx[predicted_indices == 1] = i

    # print the final results
    return last_better_idx


def print_results(results_display, dataset, bsn_results):
    auto_j_ratings = []
    for best_idx, sample in zip(bsn_results, dataset):
        results_display.write(f'Subreddit: r/{sample["subreddit"]}\n\n'
                              f'Title:\n{sample["title"]}\n\n'
                              f'Post:\n{sample["post"]}\n\n'
                              f'Best generated summary [[{best_idx}]]:\n'
                              f'{sample["summaries"][best_idx]}\n\n')
        try:
            rating = sample["autoj_eval_results"][str(best_idx)]["rating"]
            auto_j_ratings.append(rating)
            results_display.write(
                'Auto-J Comment:\n'
                f'{sample["autoj_eval_results"][str(best_idx)]["comment"]}\n\n'
            )
            results_display.write(f'Auto-J Rating: {rating}\n\n')
        finally:
            results_display.write('==========================\n\n')
            results_display.flush()

    if len(auto_j_ratings) != 0:
        results_display.write(f'{auto_j_ratings}\n\n')
        results_display.write(
            f'Average Auto-J Rating: {np.mean(auto_j_ratings)}\n\n')


@torch.no_grad()
def main():
    # Create new parser for generation
    parser = argparse.ArgumentParser()
    parser.add_argument('--gen-cfg-file',
                        dest='gen_cfg_file',
                        help='Generation config file path',
                        required=False,
                        default=None,
                        type=str)
    gen_args, extra = parser.parse_known_args()

    # Load the reward choice config
    init_cfg = global_cfg.clone()
    args = parse_args(extra)

    if args.cfg_file:
        init_cfg.merge_from_file(args.cfg_file)
    cfg_opt, client_cfg_opt = parse_client_cfg(args.opts)
    init_cfg.merge_from_list(cfg_opt)

    update_logger(init_cfg, clear_before_add=True)
    setup_seed(init_cfg.seed)

    import logging
    global logger
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.INFO)

    # Load the generation config
    gen_cfg = init_cfg.clone()
    if gen_args.gen_cfg_file:
        gen_cfg.merge_from_file(gen_args.gen_cfg_file)
    gen_cfg.freeze(save=False)

    init_cfg.freeze()

    # best_of_n dataset
    dataset = best_of_n_dataset(init_cfg,
                                gen_cfg,
                                n=16,
                                load_eval_version=True)
    dataset = dataset[:3000]
    # eval for the best_of_n dataset (vllm should be launched)
    # best_of_n_dataset_eval(init_cfg, gen_cfg, n=16)

    # get model and tokenizer
    model_name, _ = init_cfg.model.type.split('@')
    model = get_llm(init_cfg, device_map='auto')
    tokenizer, _ = get_tokenizer(model_name, init_cfg.data.root,
                                 init_cfg.llm.tok_len)

    # load model from checkpoint
    num_ckpt = \
        init_cfg.federate.total_round_num // init_cfg.federate.save_freq
    prefix = ['final_'] + \
             [str(i*init_cfg.federate.save_freq) + '_'
              for i in range(num_ckpt, -1, -1)] + ['']
    dirname, filename = os.path.split(init_cfg.federate.save_to)
    for pre in prefix:
        print(os.path.join(dirname, pre + filename))
        if os.path.exists(os.path.join(dirname, pre + filename)):
            ckpt_path = os.path.join(dirname, pre + filename)
            ckpt = torch.load(ckpt_path, map_location='cpu')
            model.load_state_dict(ckpt['model'])
            print(f'Model of Round {ckpt["cur_round"]} loads '
                  f'from the checkpoint {ckpt_path}')
            break
    # model = model.merge_and_unload()

    # get the best-of-n results and display them
    if init_cfg.llm.adapter.local_only:
        clients_results, results = \
            best_of_n_local(model, dataset, tokenizer, n=16,
                            num_clients=init_cfg.federate.client_num,
                            output_dir=init_cfg.outdir,
                            print_client_result=True)
    elif init_cfg.llm.adapter.count > 1:
        results = best_of_n_multilora(model, dataset, tokenizer, n=16)
    elif init_cfg.trainer.type == "llmpporewardtrainer":
        results = best_of_n_by_reward(model, dataset, tokenizer, n=16)
    else:
        results = best_of_n(model, dataset, tokenizer, n=16)

    path = os.path.join(init_cfg.outdir, 'test_results.txt')
    print_results(open(path, 'w'), dataset, results)
    # save the result to json file
    result_list = copy.deepcopy(dataset)
    for best_idx, sample in zip(results, result_list):
        sample['select_index'] = str(best_idx)
        sample['select_summary'] = sample['summaries'][best_idx]
        sample.pop('summaries')
    json.dump(result_list,
              open(os.path.join(init_cfg.outdir, 'test_results.json'), 'w'))


if __name__ == "__main__":
    main()
