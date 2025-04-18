import torch
import os
import numpy as np
from tqdm import tqdm
from transformers import AutoTokenizer, AutoModelForCausalLM

from vllm import LLM, SamplingParams
from federatedscope.llm.eval.eval_for_tldr.auto_j_constants_prompt \
    import build_autoj_input


def extract_single_rating(score_output):
    pred_score = 0.0
    if "Rating: [[" in score_output:
        pos = score_output.rfind("Rating: [[")
        pos2 = score_output.find("]]", pos)
        assert pos != -1 and pos2 != -1
        pred_score = float(score_output[pos + len("Rating: [["):pos2].strip())
    return pred_score


def extract_pariwise_result(raw_output):
    raw_output = raw_output.strip()
    pos = raw_output.rfind('final decision is ')
    pred_label = -1
    if pos != -1:
        pred_rest = raw_output[pos +
                               len('final decision is '):].strip().lower()
        if pred_rest.startswith('response 1'):
            pred_label = 0
        elif pred_rest.startswith('response 2'):
            pred_label = 1
        elif pred_rest.startswith('tie'):
            pred_label = 2
    return pred_label


@torch.no_grad()
def auto_j_eval_rating(dataset):
    num_gpus = torch.cuda.device_count()
    model_name_or_dir = "GAIR/autoj-13b"
    llm = LLM(model=model_name_or_dir, tensor_parallel_size=num_gpus)

    sampling_params = SamplingParams(temperature=0.0,
                                     top_p=1.0,
                                     max_tokens=1024)

    auto_j_comments, auto_j_ratings = [], []

    inputs = []

    for sample in tqdm(dataset):
        input_text = build_autoj_input(prompt=sample['query'],
                                       resp1=sample['response'],
                                       resp2=None,
                                       protocol="single")
        inputs.append(input_text)

    outputs = llm.generate(inputs, sampling_params)
    auto_j_comments = [item.outputs[0].text for item in outputs]
    auto_j_ratings = [
        extract_single_rating(judgement) for judgement in auto_j_comments
    ]

    return auto_j_comments, auto_j_ratings


@torch.no_grad()
def auto_j_eval_win_rate(dataset):
    num_gpus = torch.cuda.device_count()
    model_name_or_dir = "GAIR/autoj-13b"
    llm = LLM(model=model_name_or_dir, tensor_parallel_size=num_gpus)

    sampling_params = SamplingParams(temperature=0.0,
                                     top_p=1.0,
                                     max_tokens=1024)

    auto_j_comments, auto_j_choices = [], []

    inputs = []

    for sample in tqdm(dataset):
        input_text = build_autoj_input(prompt=sample['query'],
                                       resp1=sample['response'],
                                       resp2=sample['response_another'],
                                       protocol="pairwise_tie")
        inputs.append(input_text)

    outputs = llm.generate(inputs, sampling_params)
    auto_j_comments = [item.outputs[0].text for item in outputs]
    auto_j_choices = [
        extract_pariwise_result(judgement) for judgement in auto_j_comments
    ]

    return auto_j_comments, auto_j_choices


def read_file(path='test_results.txt'):
    f = open(path, 'r', encoding="utf8")
    colletion = []

    tag = ''
    record = {
        'subreddit': '',
        'title': '',
        'post': '',
        'response': '',
        'response_another': '',
        'score': '',
        'selector_choice': '',
        'choice': 0
    }
    for line in f.readlines():
        if 'Subreddit:' in line:
            record['subreddit'] = line.replace('Subreddit: ', '')
            tag = 'subreddit'

        elif 'Title:' in line:
            tag = 'title'

        elif 'Post:' in line:
            tag = 'post'

        elif 'generated summary' in line:
            tag = 'response'
            if '[[' in line:
                pos = line.rfind("[[")
                pos2 = line.find("]]", pos)
                record['choice'] = int(line[pos + len("[["):pos2].strip())

        elif 'Human summary' in line:
            tag = 'response_another'

        elif 'Score' in line:
            tag = 'score'

        elif 'Selector choice' in line:
            tag = 'selector_choice'

        elif '=============' in line:
            # The end of the record, which should be
            # appended to the collection
            for key in record.keys():
                if type(record[key]) is str and record[key].endswith('\n\n'):
                    record[key] = record[key][:-2]
            query = ("Summarize the following post\n\n"
                     "Title: {title}\n\n"
                     "Post: {post}").format_map(record)
            record['query'] = query
            colletion.append(record)
            record = {
                'subreddit': '',
                'title': '',
                'post': '',
                'response': '',
                'response_another': '',
                'score': '',
                'selector_choice': '',
                'choice': 0
            }

        else:
            # This is a normal line and should be
            # extended to current tag of record
            record[tag] = record[tag] + line

    return colletion


def evaluation(file_path):
    dataset = read_file(file_path)
    auto_j_comments, auto_j_ratings = auto_j_eval_rating(dataset)

    # print the evaluation results
    with open(file_path + '_autoj_eval.txt', 'w') as f:
        for sample, comment, rating in zip(dataset, auto_j_comments,
                                           auto_j_ratings):
            f.write(f'Subreddit: {sample["subreddit"]}\n\n'
                    f'Title:\n{sample["title"]}\n\n'
                    f'Post:\n{sample["post"]}\n\n'
                    f'Best generated summary:\n{sample["response"]}\n\n'
                    f'Auto-J Comment:\n{comment}\n\n'
                    f'Auto-J Rating: {rating}\n\n')
            f.write('==========================\n\n')
            f.flush()
        f.write(f'{auto_j_ratings}\n\n')
        f.write(f'Average Auto-J Rating: {np.mean(auto_j_ratings)}\n\n')

    return auto_j_ratings, np.mean(auto_j_ratings)


def eval_win_rate(file_path):
    dataset = read_file(file_path)
    auto_j_comments, auto_j_choices = auto_j_eval_win_rate(dataset)

    # print the evaluation results
    with open(file_path + '_autoj_eval_win.txt', 'w') as f:
        for sample, comment, choice in zip(dataset, auto_j_comments,
                                           auto_j_choices):
            f.write(f'Subreddit: {sample["subreddit"]}\n\n'
                    f'Title:\n{sample["title"]}\n\n'
                    f'Post:\n{sample["post"]}\n\n'
                    f'Best generated summary:\n{sample["response"]}\n\n'
                    f'Human summary:\n{sample["response_another"]}\n\n'
                    f'Auto-J Comment:\n{comment}\n\n'
                    f'Auto-J Selection: {choice}\n\n')
            f.write('==========================\n\n')
            f.flush()
        f.write(f'{auto_j_choices}\n\n')
        win = np.sum(np.array(auto_j_choices) == 0)
        lose = np.sum(np.array(auto_j_choices) == 1)
        tie = np.sum(np.array(auto_j_choices) == 2)
        rate = win / (win + lose + tie) * 100
        f.write(f'Win Rate (win/lose/tie): {rate}% ({win}/{lose}/{tie})\n\n')

    return auto_j_choices, rate


def evaluation_multiple_clients(dir, clients):
    clients_choice, datasets = [], []
    for client in clients:
        datasets.append(
            read_file(os.path.join(dir, f'test_results_client_{client}.txt')))
        clients_choice.append([record['choice'] for record in datasets[-1]])

    array = np.array(clients_choice).T
    majority_votes_idx = [
        np.bincount(array[i]).argmax() for i in range(len(array))
    ]

    best_response = [[] for _ in majority_votes_idx]
    for dataset in datasets:
        for idx, sample in enumerate(dataset):
            if sample['choice'] == majority_votes_idx[idx]:
                best_response[idx] = sample

    auto_j_comments, auto_j_ratings = auto_j_eval_rating(best_response)

    # print the evaluation results
    with open(dir + '/selected_autoj_eval.txt', 'w') as f:
        for sample, comment, rating in zip(best_response, auto_j_comments,
                                           auto_j_ratings):
            f.write(f'Subreddit: {sample["subreddit"]}\n\n'
                    f'Title:\n{sample["title"]}\n\n'
                    f'Post:\n{sample["post"]}\n\n'
                    f'Best generated summary:\n{sample["response"]}\n\n'
                    f'Auto-J Comment:\n{comment}\n\n'
                    f'Auto-J Rating: {rating}\n\n')
            f.write('==========================\n\n')
            f.flush()
        f.write(f'{auto_j_ratings}\n\n')
        f.write(f'Average Auto-J Rating: {np.mean(auto_j_ratings)}\n\n')

    return auto_j_ratings, np.mean(auto_j_ratings)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--pairwise', dest='pairwise', action="store_true")
    parser.add_argument('--model-outputs-file',
                        dest='file',
                        help='Path to model-generated outputs',
                        type=str)
    parser.add_argument('--model-outputs-directory',
                        dest='dir',
                        help='Directory to model-generated outputs',
                        type=str)
    parser.add_argument('--clients',
                        dest='clients',
                        help='Selected clients for evaluation',
                        required=False,
                        nargs='+',
                        type=int)
    args = parser.parse_args()

    # dataset = read_file(args.file)
    if args.clients:
        evaluation_multiple_clients(args.file, args.clients)
    elif args.pairwise:
        eval_win_rate(args.file)
    elif args.dir is not None:
        for base_dir, _, files in os.walk(args.dir):
            print(base_dir, files)
            for filename in files:
                if filename.endswith('_summarization.txt'):
                    file_path = os.path.join(base_dir, filename)
                    print(f'>>>> I am running for {file_path}')
                    if (filename + '_autoj_eval_win.txt' not in files):
                        eval_win_rate(file_path)
                    # if (filename + '_autoj_eval.txt' not in files):
                    #     evaluation(file_path)
    else:
        evaluation(args.file)
        eval_win_rate(args.file)
