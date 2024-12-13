# Copyright 2022 Cerebras Systems.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import copy
import json
import logging
import os
import re
import sys
from collections import OrderedDict, defaultdict
from pathlib import Path
from typing import Dict, Optional

import numpy as np
import yaml

logger = logging.getLogger("utils")
logger.setLevel(logging.INFO)

## Added .parquet extension to the list of valid extensions
VALID_EXTENSIONS = [
    '.jsonl',
    '.jsonl.zst',
    '.jsonl.zst.tar',
    '.txt',
    '.json.gz',
    '.parquet',
    '.fasta',
]


SYSTEM_PROMPT_REGISTRY = {
    "zephyr": "<|system|>\n</s>",
    "vicuna_v0": (
        "A chat between a curious human and an artificial intelligence assistant. "
        "The assistant gives helpful, detailed, and polite answers to the human's questions."
    ),
    "vicuna_v1": (
        "A chat between a curious user and an artificial intelligence assistant. "
        "The assistant gives helpful, detailed, and polite answers to the user's questions."
    ),
    "llava_plain": "",
    "llava_v0": (
        "A chat between a curious human and an artificial intelligence assistant. "
        "The assistant gives helpful, detailed, and polite answers to the human's questions."
    ),
    "llava_v1": (
        "A chat between a curious human and an artificial intelligence assistant. "
        "The assistant gives helpful, detailed, and polite answers to the human's questions."
    ),
    "mistral_instruct": "",
}


def has_valid_extension(file):
    return any([file.endswith(ext) for ext in VALID_EXTENSIONS])


def _listdir_or_file(x):
    if isinstance(x, list):
        return reduce(lambda x, y: x + y, map(listdir_or_file, sorted(x)))
    if os.path.isfile(x):
        return [x]
    elif os.path.isdir(x):
        return [str(Path(x) / fn) for fn in sorted(os.listdir(x))]
    else:
        raise FileNotFoundError(f"{x} not found")


def listdir_or_file(x):
    return list(filter(has_valid_extension, _listdir_or_file(x)))


def dump_result(
    results,
    json_params_file,
    eos_id=None,
    pad_id=None,
    vocab_size=None,
):
    """
    Write outputs of execution
    """
    with open(json_params_file, "r") as _fin:
        data = json.load(_fin)

    post_process = {}
    post_process["discarded_files"] = results.pop("discarded", 0)
    post_process["processed_files"] = results.pop("processed", 0)
    post_process["successful_files"] = results.pop("successful", 0)
    post_process["n_examples"] = results.pop("examples", 0)
    post_process["raw_chars_count"] = results.pop("raw_chars_count", 0)
    post_process["raw_bytes_count"] = results.pop("raw_bytes_count", 0)
    results.pop("features")
    ## put remaining key,value pairs in post process
    for key, value in results.items():
        post_process[key] = value

    if eos_id is not None:
        post_process["eos_id"] = eos_id
    if pad_id is not None:
        post_process["pad_id"] = pad_id
    if vocab_size is not None:
        post_process["vocab_size"] = vocab_size

    data["post-process"] = post_process
    with open(json_params_file, "w") as _fout:
        json.dump(data, _fout, indent=4, sort_keys=True)


def dump_args(args, json_params_file):
    """
    Write the input params to file.
    """
    logger.info(f"User arguments can be found at {json_params_file}.")

    redundant_params = [
        "eos_id",
        "pad_id",
        "display_pbar",
        "files_per_record",
        "output_name",
        "write_remainder",
    ]

    relevant_args = copy.deepcopy(args)
    # Iterate through the dictionary and remove the redundant params
    for key in redundant_params:
        for sub_dict in relevant_args.values():
            if key in sub_dict:
                del sub_dict[key]

    # write initial params to file
    with open(json_params_file, "w") as _fout:
        json.dump(args, _fout, indent=4, sort_keys=True)


def update_args(args, json_params_file):
    "Update eos_id and pad_id in data_params"

    with open(json_params_file, "r") as _file:
        data = json.load(_file)

    data['processing']['pad_id'] = args.get(
        'pad_id', data['processing'].get('pad_id')
    )
    data['processing']['eos_id'] = args.get(
        'eos_id', data['processing'].get('eos_id')
    )
    data['features'] = args.get('features', None)

    with open(json_params_file, "w") as _fout:
        json.dump(data, _fout, indent=4, sort_keys=True)


def get_parser(desc):
    """Argparser definition for command line arguments from user.

    Returns:
        Argparse namespace object with command line arguments.
    """
    parser = argparse.ArgumentParser(description=desc)
    add_preprocess_args(parser)
    return parser.parse_args()


def add_preprocess_args(parser):
    """Add arguments to the data preprocessing parser."""
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to the YAML config file for setting dataset preprocessing hyper-parameters.",
    )


def update_params(params, args):
    """
    Update config parameters with CLI arguments
    """
    setup_params = [
        "data",
        "metadata_files",
        "output_dir",
        "image_dir",
        "processes",
        "mode",
    ]
    processing_params = [
        "custom_tokenizer",
        "huggingface_tokenizer",
        "tokenizer_params",
        "eos_id",
        "pad_id",
        "max_seq_length",
        "min_sequence_len",
        "input_ids_dtype",
        "input_mask_dtype",
        "inverted_mask",
        "use_ftfy",
        "ftfy_normalizer",
        "wikitext_detokenize",
        "short_seq_prob",
        "write_in_batch",
        "resume_from_checkpoint",
        "seed",
        "read_chunk_size",
        "write_chunk_size",
        "shuffle",
        "shuffle_seed",
        "fraction_of_RAM_alloted",
        "read_hook",
        "read_hook_kwargs",
        "semantic_drop_mask",
        "semantic_loss_weight",
        "semantic_attention_mask",
    ]
    dataset_params = [
        "use_vsl",
        "truncate_to_msl",
        "max_prompt_length",
        "is_multimodal",
        "training_objective",
        "pack_sequences",
        "sep_token",
        "fim_rate",
        "spm_rate",
        "fim_prefix_tok",
        "fim_middle_tok",
        "fim_suffix_tok",
        "fold_long_doc",
        "split_text_to_tokenize",
        "chunk_len_to_split",
        "remove_bos_in_chunks",
        "user_role",
        "assistant_role",
        "chat_template",
        "respose_delimiter",
        "num_patches",
        "mlm_fraction",
        "mlm_with_gather",
        "ignore_index",
        "excluded_tokens",
        "max_num_img",
    ]
    cli_params = [
        "cmd",
        "func",
    ]

    for key, value in args.items():
        if value in ["True", "False"]:
            value = value == "True"
        if value is not None:
            if key in setup_params:
                params["setup"][key] = value
            elif key in processing_params:
                params["processing"][key] = value
            elif key in dataset_params:
                params["dataset"][key] = value
            elif key in cli_params:
                continue
            else:
                raise ValueError(f"Unexpected arguments: {key}")

    # Sections to check
    sections = {
        "setup": setup_params,
        "processing": processing_params,
        "dataset": dataset_params,
    }

    for section, allowed_params in sections.items():

        params_in_yaml = params.get(section, {})

        # Check for misplaced parameters
        for param in params_in_yaml:
            if param not in allowed_params:
                correct_section = next(
                    (s for s, p in sections.items() if param in p),
                    "unknown section",
                )
                if correct_section != "unknown section":
                    raise ValueError(
                        f"Error: Parameter '{param}' in section '{section}' is misplaced. It should be in '{correct_section}'."
                    )


def args_to_params(args):
    """Process data preprocessing CLI arguments to parameters
    Returns:
        params (Dict): Dictionary contains the parameters used to configure
            the data processing.
    """
    args = vars(args)

    params_file = args.pop("config", None)
    if params_file:
        with open(params_file, 'r') as stream:
            params = yaml.safe_load(stream)
    else:
        params = {}

    for section in ["setup", "processing", "dataset"]:
        if not params.get(section, None):
            params[section] = {}

    update_params(params, args)
    return params


def get_params(desc):
    """Retrieve configuration parameters
    Returns:
        params (Dict): Dictionary contains the parameters used to configure
            the data processing.
    """
    args = get_parser(desc)
    return args_to_params(args)


def dump_args(args, json_params_file):
    """
    Write the input params to file.
    """
    # write initial params to file
    with open(json_params_file, "w") as _fout:
        json.dump(args, _fout, indent=4, sort_keys=True)


def setup_warning_logging(output_dir, module_name):
    """
    Set up logging to log warnings to a file in the specified output directory.

    Args:
        output_dir (str): The directory where the warnings log file should be stored.
    """
    logger = logging.getLogger(module_name)
    logger.setLevel(logging.INFO)
    os.makedirs(output_dir, exist_ok=True)
    # Create a file handler that logs to 'output_dir/warnings.log'
    log_file_path = os.path.join(output_dir, 'warnings.log')
    file_handler = logging.FileHandler(log_file_path)

    # Create a formatter and set it for the file handler
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    file_handler.setFormatter(formatter)

    # Add the file handler to the logger
    logger.addHandler(file_handler)
    # Remove the default StreamHandler to prevent logging to stdout
    logger.propagate = False

    return logger


def get_files(input_dir=None, filetypes=None, metadata_files=None):
    """Get all files of given filetypes from input directory.

    Args:
        input_dir (str): Input directory to read files from.
        filetypes (list): File types to fetch from the given input
            directory. Defaults to `None`.
        metadata_files (str): Comma separated string of metadata files.

    Returns:
        List of lists containing all file paths as strings
    """
    if not filetypes:
        filetypes = [
            '.jsonl',
            '.json.gz',
            '.jsonl.zst',
            '.jsonl.zst.tar',
            '.txt',
            '.parquet',
            '.fasta',
        ]
    if isinstance(filetypes, str):
        filetypes = [filetypes]
    filetypes = tuple(filetypes)
    assert input_dir or metadata_files, (
        "User need to provide `input_dir` or `metadata_files`, "
        "but neither was provided."
    )
    if metadata_files:
        if isinstance(metadata_files, str):
            metadata_files = [metadata_files]

        if input_dir:
            logger.warning(
                "Both `input_dir` and `metadata_files` were provided, "
                "ignoring `input_dir` and using `metadata_files`."
            )

        input_files = []
        for _file in metadata_files:
            with open(_file, "r") as _fin:
                input_files.extend(_fin.readlines())

        input_files_list = [x.strip() for x in input_files if x]
        flattened_list = [x for x in input_files_list if x.endswith(filetypes)]
    else:
        files = [list(Path(input_dir).rglob(f"*{ft}")) for ft in filetypes]
        # flatten list of list -> list and stringify Paths
        flattened_list = [str(item) for sublist in files for item in sublist]
    if not flattened_list:
        raise Exception(
            f"Did not find any files at this path {input_dir}, please "
            f"ensure your files are in format {filetypes}."
        )
    return flattened_list


def wikitext_detokenizer(string):
    """Detokenizer for wikitext. Used for special handling of data for substrings.

    Args:
        string (str): String to detoknize before tokenization.

    Returns:
        Detokenized string
    """
    # contractions
    string = string.replace("s '", "s'")
    string = re.sub(r"/' [0-9]/", r"/'[0-9]/", string)
    # number separators
    string = string.replace(" @-@ ", "-")
    string = string.replace(" @,@ ", ",")
    string = string.replace(" @.@ ", ".")
    # punctuation
    string = string.replace(" : ", ": ")
    string = string.replace(" ; ", "; ")
    string = string.replace(" . ", ". ")
    string = string.replace(" ! ", "! ")
    string = string.replace(" ? ", "? ")
    string = string.replace(" , ", ", ")
    # double brackets
    string = re.sub(r"\(\s*([^\)]*?)\s*\)", r"(\1)", string)
    string = re.sub(r"\[\s*([^\]]*?)\s*\]", r"[\1]", string)
    string = re.sub(r"{\s*([^}]*?)\s*}", r"{\1}", string)
    string = re.sub(r"\"\s*([^\"]*?)\s*\"", r'"\1"', string)
    string = re.sub(r"'\s*([^']*?)\s*'", r"'\1'", string)
    # miscellaneous
    string = string.replace("= = = =", "====")
    string = string.replace("= = =", "===")
    string = string.replace("= =", "==")
    string = string.replace(" " + chr(176) + " ", chr(176))
    string = string.replace(" \n", "\n")
    string = string.replace("\n ", "\n")
    string = string.replace(" N ", " 1 ")
    string = string.replace(" 's", "'s")

    return string


def clean_text(
    data: str, use_ftfy: bool, wikitext_detokenize: bool, ftfy_normalizer: str
) -> str:
    """
    Clean the provided text using ftfy normalization and wikitext detokenization.

    Args:
        data (str): The text to be cleaned.
        use_ftfy (bool): Whether to use the `ftfy` library to fix text encoding issues.
        wikitext_detokenize (bool): Whether to apply wikitext detokenization to the text.
        ftfy_normalizer (str): The normalization method to use with `ftfy` if enabled.

    Returns:
        str: The cleaned text after applying the specified operations.
    """
    import ftfy

    if use_ftfy:
        data = ftfy.fix_text(data, normalization=ftfy_normalizer)
    if wikitext_detokenize:
        data = wikitext_detokenizer(data)

    return data


def get_data_stats(
    sample: np.ndarray,
    pad_id: int,
    eos_id: int,
    max_seq_length: int,
    loss_valid_tokens: Optional[int] = None,
) -> Dict[str, int]:
    """
    Get data statistics from the sample.

    Args:
        sample (np.ndarray): Tokenized sample in the form of a NumPy array.
        pad_id (int): The ID used for padding tokens.
        eos_id (int): The ID used for end-of-sequence tokens.
        max_seq_length (int): The maximum sequence length.
        loss_valid_tokens (Optional[int]): The number of valid tokens for loss computation. If not provided, it will be calculated from the sample.

    Returns:
        Dict[str, int]: A dictionary containing the following data statistics:
            - "num_pad_tokens": Number of padding tokens in the sample.
            - "non_pad_tokens": Number of tokens that are neither padding nor end-of-sequence tokens.
            - "num_tokens": Total number of tokens in the sample.
            - "loss_valid_tokens": Number of valid tokens for loss computation.
            - "num_masked_tokens": Number of masked tokens based on the maximum sequence length.
    """
    stats = defaultdict(int)
    if sample == []:
        return stats
    stats["num_pad_tokens"] = int((sample[0, :] == pad_id).sum())
    stats["non_pad_tokens"] = int(
        np.logical_and(sample[0, :] != eos_id, sample[0, :] != pad_id).sum()
    )
    stats["num_tokens"] = int(sample[0, :].shape[0])

    if loss_valid_tokens:
        stats["loss_valid_tokens"] = loss_valid_tokens
    else:
        stats["loss_valid_tokens"] = int(sample[1, :].sum())
    stats["num_masked_tokens"] = max_seq_length - stats["loss_valid_tokens"]

    return stats


# routine to split the text into smaller sequences
def split_text_and_tokenize(
    text, tokenizer, max_tok_len=2000, remove_bos_in_chunks=True
):
    """Function to split the text into smaller sequences of length max_tok_len
    and then tokenize each of the smaller sequences. This is done to avoid
    performance issues with tokenizers like LlamaTokenizer which are slow for
    long sequences.

    Args:
        text (str): text to be tokenized
        tokenizer (Tokenizer): tokenizer to be used
        max_tok_len (int, optional): max length of each sequence. Defaults to 2000.
        remove_bos_in_chunks (bool, optional): whether to ignore bos token id in
            chunks. Defaults to True.
    Returns:
        tok_ids (list): list of token ids for the text
    """
    if len(text) == 0:
        return []

    curr_start = 0
    tok_ids = []

    while curr_start < len(text):
        curr_end = min(text.find(' ', curr_start + max_tok_len), len(text))
        if curr_end < 0:
            curr_substr = text[curr_start:]
            curr_end = len(text)
        else:
            curr_substr = text[curr_start:curr_end]
        if curr_start == 0:
            # keep special tokens for the first chunk
            bos_token_id = [tokenizer.encode(curr_substr)[0]]
        curr_tok_ids = (
            tokenizer.encode(curr_substr)[1:]
            if remove_bos_in_chunks
            else tokenizer.encode(curr_substr)
        )
        tok_ids.extend(curr_tok_ids)
        curr_start = curr_end
    # concatenated tok_ids chunks together by using `extend` to return full sequence of tokens

    # NOTE: add bos token id if it is needed here, eos id is added in the next line
    # which calls this function
    return bos_token_id + tok_ids if remove_bos_in_chunks else tok_ids


def chunk(
    sample,
    tokenizer,
    fim_rate,
    spm_rate,
):
    """
    Since we do character-level FIM we need to detokenize, determine boundaries
    to split, and re-tokenize after splitting. We chunk but do not shuffle and add
    special tokens because we might have to truncate or pad the tokens since they
    have been split at the character-level and re-tokenized, leading to potentially
    different lengths than the original sequence.
    If the sub-context is designated to be an AR (auto-regressive) sequence and not FIM, we store
    as [[], [], [sequence]] for convenience in the truncate_helper function.

    Args:
        sample (np.array):
        tokenizer (Tokenizer):
        fim_rate (float):
        spm_rate (float):

    Returns:
        List[List[int]], str: List of token lists corresponding to the
          prefix/middle/suffix tokens, or 2 empty lists plus the whole
          sequence in case of auto-regressive (AR) sequence. Also returns
          string representing the format of the sequence (i.e. SPM or
          PSM or AR)
    """
    if np.random.binomial(1, fim_rate):  # sample bernoulli dist
        contents = tokenizer.decode(sample, skip_special_tokens=False)
        try:
            # A boundary can be =0 (prefix will be empty)
            # a boundary can be =len(contents) (suffix will be empty)
            # The two boundaries can be equal (middle will be empty)
            boundaries = list(
                np.random.randint(low=0, high=len(contents) + 1, size=2)
            )
            boundaries.sort()
        except ValueError as e:
            logging.info(len(contents))
            logging.info(contents)
            logging.info(e)
            raise e

        prefix = contents[: boundaries[0]]
        middle = contents[boundaries[0] : boundaries[1]]
        suffix = contents[boundaries[1] :]

        prefix = tokenizer.encode(prefix)
        middle = tokenizer.encode(middle)
        suffix = tokenizer.encode(suffix)

        is_spm = np.random.binomial(1, spm_rate)
        fim_format = "SPM" if is_spm else "PSM"
        return [prefix, middle, suffix], fim_format
    else:
        # don't do FIM preproc
        fim_format = "AR"
        return [[], [], sample.tolist()], fim_format


def format_fim(
    segment_fim_format_pairs,
    max_seq_len,
    suffix_tok_id,
    prefix_tok_id,
    middle_tok_id,
    eos_tok_id,
    opt_bos_tok_id,
):
    """
    Takes in list of prefix/middle/suffix token lists, along with respective FIM (or AR) formats.
    Performs the correct transformation according to the format, adding the special tokens
    and shuffling the sections, before concatenating everything together.

    Args:
        segments_fim_format_pairs (List[Tuple[List[List[int]], str]]): This list of tuples is used
        to store the prefix/middle/suffix token-id lists and the corresponding FIM formats (PSM/SPM) to
        be used downstream in the FIM formatting.
        max_seq_len (int): Max sequence length that each sequence is expected
          to match
        suffix_tok_id (int): Id for suffix token
        prefix_tok_id (int): Id for suffix token
        middle_tok_id (int): Id for suffix token
        eos_tok_id (int): Id for suffix token
        opt_bos_tok_id (list): Optionally a list containing the bos token id,
          otherwise will be empty list. Empty list will be a no-op in the
          concatenation. Bos-token will only exist if model's tokenizer adds
          bos-token by default. Both have to be lists so that np concat works

    Returns:
        sample (np.array): Array of token ids in the FIMed order
          along with special tokens
        mask (np.array): Array of 1's and 0's corresponding to true
          tokens and padding respectively
        label (np.array): Token i of label corresponds to token i+1 in
          sample array. Same elements except that label ends in eos
          (end-of-sequence) token
    """

    prefix_idx, middle_idx, suffix_idx = 0, 1, 2
    sample = []
    total_padding_len = 0
    for sample_i, fim_format in segment_fim_format_pairs:
        optional_padding = sample_i[-1] if len(sample_i) > 3 else []
        total_padding_len += len(optional_padding)
        if fim_format == "PSM":
            sample_i = np.concatenate(
                [
                    opt_bos_tok_id,
                    [prefix_tok_id],
                    sample_i[prefix_idx],
                    [suffix_tok_id],
                    sample_i[suffix_idx],
                    [middle_tok_id],
                    sample_i[middle_idx],
                    [eos_tok_id],
                ]
            )
        elif fim_format == "SPM":
            sample_i = np.concatenate(
                [
                    opt_bos_tok_id,
                    [prefix_tok_id, suffix_tok_id],
                    sample_i[suffix_idx],
                    [middle_tok_id],
                    sample_i[prefix_idx],
                    sample_i[middle_idx],
                    [eos_tok_id],
                ]
            )
        else:
            sample_i = np.concatenate(
                [
                    opt_bos_tok_id,
                    sample_i[prefix_idx],
                    sample_i[middle_idx],
                    sample_i[suffix_idx],
                    [eos_tok_id],
                ]
            )
        sample_i = np.concatenate([sample_i, optional_padding])
        sample.append(sample_i)
    sample = np.concatenate(sample).astype(np.int64)
    label = sample[1:]
    sample = sample[:-1]
    sample_mask = np.ones(max_seq_len - total_padding_len)
    padding_mask = np.zeros(total_padding_len)
    mask = np.concatenate([sample_mask, padding_mask])
    return sample, mask, label


def truncate_helper(samples_lst, diff, sample_idx):
    """
    The goal of our truncation scheme is to avoid removing tokens from the
    middle section. We first remove from the end of suffix, and then from the
    beginning of the prefix. We store the chunks in lists in the original order
    so that we can easily perform this truncation. Since each sub-context can have
    different amounts of tokens in suffix/prefix, we store unique indices for the
    section to remove from. If we run out of tokens to remove from, we switch to the next.
    This way we can switch to the prefix of one context while still removing from suffix
    of another. If the sub-context is AR (auto-regressive) and not FIM, the AR sequence
    is stored as [[], [], [sequence]] so that the remove_idx being 2 will simultaneously
    work for the AR and FIM sequences.

    Args:
        samples_lst (List[List[int]]): List of lists that contain token ids
        diff (int): Number of tokens to pad
        sample_idx (int): Index for the sample from the dataset, for use in
          logging if we remove from the middle.

    Returns:
        (List[List[int]]): List of lists of token ids that have been truncated
    """
    num_groups = len(samples_lst)
    remove_idxs = [2] * num_groups  # remove from suffixes first
    i = 0

    while diff:
        remove_idx_i = remove_idxs[i]
        sample_i = samples_lst[i]
        if sample_i[remove_idx_i]:
            pop_idx = (
                -1 if remove_idx_i == 2 else 0
            )  # remove from end of suffix but beginning of prefix
            sample_i[remove_idx_i].pop(pop_idx)
            diff -= 1
        else:
            remove_idxs[i] = (
                remove_idxs[i] + 1
            ) % 3  # order of removal is end of suffix, beginning of prefix, then beginning of middle
            if remove_idxs[i] == 1:
                logging.info(
                    f"""Context {i} in the {sample_idx}-th data sample has
                        begun truncating from the middle section, meaning
                        the prefix and suffix sections have been exhausted.
                      """
                )
        i = (i + 1) % num_groups

    return samples_lst


def pad_helper(samples_lst, diff, fim_pad_tok_id):
    """
    Helper for padding. We put all padding tokens into the last sequence.

    Args:
        samples_lst (List[List[int]]): List of lists that contain token ids
        diff (int): Number of tokens to pad
        fim_pad_tok_id (int): Id for padding token

    Returns:
        (List[List[int]]): List of lists of token ids with padding
    """
    padding = np.full(np.abs(diff), fim_pad_tok_id)
    samples_lst[-1].append(padding)
    return samples_lst


def truncate_or_pad_helper(
    segments_fim_format_pairs, diff, fim_pad_tok_id, sample_idx
):
    """
    Since we perform FIM at character-level, we potentially split characters
    in the middle of a word. This can lead to non-standard token sequences,
    and after re-tokenizing we might need to truncate or pad to get back to
    the original context length. This function ensures that our outputs are
    back at their original length.

    Args:
        segments_fim_format_pairs (List[Tuple[List[List[int]], str]]): This list of tuples is used
        to store the prefix/middle/suffix token-id lists and the corresponding FIM formats (PSM/SPM) to
        be used downstream in the FIM formatting.
        diff (int): The number of tokens to add or remove. Positive means truncate, negative means pad
        fim_pad_tok_id (int): Id of padding token

    Returs:
        (List[Tuple[List[List[int]], str]]): The element of the tuples will
        now be lists that are truncated or padded such that the concatenation of all these tokens, along
        with the special tokens, will be equal to the original sequence length.
    """
    segments = [pair[0] for pair in segments_fim_format_pairs]
    fim_formats = [pair[1] for pair in segments_fim_format_pairs]
    if diff >= 0:
        segments = truncate_helper(segments, diff, sample_idx)
    else:
        segments = pad_helper(segments, diff, fim_pad_tok_id)
    return [(segments[i], fim_formats[i]) for i in range(len(segments))]


def fim(
    sample_array,
    sample_idx,
    tokenizer,
    fim_rate,
    spm_rate,
    suffix_tok_id,
    prefix_tok_id,
    middle_tok_id,
    fim_pad_tok_id,
    eos_tok_id,
    opt_bos_tok_id,
):
    """
    Takes in an array of input_ids, mask, and labels, and performs the
    FIM operation to re-arrange into PSM and SPM format with some probability

    Args:
        sample_array (np.array): Stack of input_ids, mask, and labels after tokenization. Labels are off-by-one of input_ids
        as in standard auto-regressive training
        i (int): Index of sample from dataset, used for logging.
        tokenizer (Tokenizer): Tokenizer object
        fim_rate (float): Determines what percentage of contexts are FIM'ed
        spm_rate (float): Determines what percentage of FIM'ed contexts are in SPM format. 1 - spm_rate determines PSM
        suffix_tok_id (int): Id for special token denoting suffix section in a FIM'ed context
        prefix_tok_id (int): Id for special token denoting prefix section in a FIM'ed context
        middle_tok_id (int): Id for special token denoting middle section in a FIM'ed context
        fim_pad_tok_id (int): Id for padding
        eos_tok_id (int): Id for the end-of-seqence
        opt_bos_tok_id (list): Optionally a list containing the bos token id,
          otherwise will be empty list. Empty list will be a no-op in the
          concatenation. Bos-token will only exist if model's tokenizer adds
          bos-token by default.

    Returns:
        fim_outputs (np.array): Stack of input_ids, mask, and labels after FIM transformation. Mask and labels have been
        adjusted to still filter padding tokens and represent the following token, respectively.
    """
    assert (
        fim_rate <= 1 and fim_rate >= 0
    ), "FIM rate must be a probability 0 <= rate <= 1"
    sample = sample_array[0, :]
    mask = sample_array[1, :]
    max_seq_len = sample.shape[0]

    segment_breaks = np.argwhere(
        sample == eos_tok_id
    )  # split sample by document
    segments_fim_format_pairs = []
    if segment_breaks.shape != (0, 1):  # FIM each sub-context
        curr_start_position = 0
        for loc in np.nditer(segment_breaks):
            # Only permute non-empty segments.
            if loc - curr_start_position > 0:
                segments, fim_format = chunk(
                    sample=sample[curr_start_position:loc],
                    tokenizer=tokenizer,
                    fim_rate=fim_rate,
                    spm_rate=spm_rate,
                )
                segments_fim_format_pairs.append((segments, fim_format))
            curr_start_position = loc + 1  # jump over the EOD token
        # Permute the segment after the last EOD
        segments, fim_format = chunk(
            sample=sample[curr_start_position:],
            tokenizer=tokenizer,
            fim_rate=fim_rate,
            spm_rate=spm_rate,
        )
        segments_fim_format_pairs.append((segments, fim_format))
    else:  # FIM over full context
        segments, fim_format = chunk(
            sample=sample,
            tokenizer=tokenizer,
            fim_rate=fim_rate,
            spm_rate=spm_rate,
        )
        segments_fim_format_pairs.append((segments, fim_format))

    def flatten_2d(arr):
        return np.concatenate([np.concatenate(subarr) for subarr in arr])

    total_len = flatten_2d(
        [pair[0] for pair in segments_fim_format_pairs]
    ).shape[0]
    # we factor in the final EOS, which we add before splitting into
    # inputs and labels, i.e. sequence[:-1] and sequence[1:], and the
    # optional bos token
    add_constant = -1
    for _, fmt in segments_fim_format_pairs:
        if fmt == "AR":
            add_constant += 1
        else:
            add_constant += 4
        if opt_bos_tok_id:
            add_constant += 1
    diff = (total_len + add_constant) - max_seq_len
    segments_fim_format_pairs = truncate_or_pad_helper(
        segments_fim_format_pairs,
        diff,
        fim_pad_tok_id,
        sample_idx,
    )
    inputs, mask, labels = format_fim(
        segments_fim_format_pairs,
        max_seq_len,
        suffix_tok_id,
        prefix_tok_id,
        middle_tok_id,
        eos_tok_id,
        opt_bos_tok_id,
    )

    try:
        assert inputs.shape[0] == max_seq_len
        assert mask.shape[0] == max_seq_len
        assert labels.shape[0] == max_seq_len
    except:
        logging.error(
            "The inputs/masks/labels were not the correct\
                      sized after FIM process. Shapes of each are printed\
                      below, along with the correct max seqeunce length\
                      that each sequence should be."
        )
        logging.error(inputs.shape, max_seq_len)
        logging.error(mask.shape, max_seq_len)
        logging.error(labels.shape, max_seq_len)
        raise AssertionError
    try:
        assert labels[-1] == eos_tok_id
    except:
        logging.error("The sequence did not end with an EOS token")
        raise AssertionError
    # end FIM-specific code
    fim_outputs = np.stack([inputs, mask, labels], axis=0)
    return fim_outputs


def get_tokenizer_vocab(tokenizer):
    from transformers import PreTrainedTokenizer, PreTrainedTokenizerFast

    from cerebras.modelzoo.data_preparation.nlp.tokenizers.BPETokenizer import (
        BPETokenizer,
    )
    from cerebras.modelzoo.data_preparation.nlp.tokenizers.HFTokenizer import (
        HFTokenizer,
    )

    if isinstance(tokenizer, BPETokenizer):
        tokenizer_vocab = tokenizer.encoder
    elif isinstance(tokenizer, HFTokenizer):
        tokenizer_vocab = tokenizer.tokenizer.get_vocab()
    elif isinstance(tokenizer, PreTrainedTokenizer) or isinstance(
        tokenizer, PreTrainedTokenizerFast
    ):
        tokenizer_vocab = tokenizer.vocab
    else:
        raise NotImplementedError(
            "We do not support specified tokenizer\
                                  type."
        )
    return tokenizer_vocab


def check_fim_special_tokens(params, tokenizer):
    # Check that input config lists the FIM special tokens
    assert (
        "fim_suffix_tok" in params['dataset']
        and "fim_prefix_tok" in params['dataset']
        and "fim_middle_tok" in params['dataset']
    ), """Configs for FIM pre-processing must include the special tokens that
    denote prefix, middle, and suffix tokens."""
    # Check that the provided tokens are in the tokenizer
    pre_tok = params['dataset'].get("fim_prefix_tok")
    mid_tok = params['dataset'].get("fim_middle_tok")
    suf_tok = params['dataset'].get("fim_suffix_tok")
    tokenizer_vocab = get_tokenizer_vocab(tokenizer)
    assert (
        pre_tok in tokenizer_vocab
        and mid_tok in tokenizer_vocab
        and suf_tok in tokenizer_vocab
    ), """Please ensure that the provided FIM special tokens are in the
    specified tokenizer."""


def handle_bos_token_default(tokenizer):
    """
    When performing FIM, we tokenize each chunk again after splitting.
    Therefore, if the tokenizer adds bos-token by default, we will get
    extra bos-tokens in the middle of the sequence. In this function,
    we set the tokenizer bos default to False, and return a flag that
    indicates whether we will need to add bos-token in the final
    fim formatting function.
    """
    if hasattr(tokenizer, "add_bos_token") and tokenizer.add_bos_token:
        tokenizer.add_bos_token = False
        bos_tok_id = tokenizer.encode(tokenizer.bos_token)[-1]
        return True, [bos_tok_id]
    return False, []


def get_size(obj, seen=None):
    """Recursively finds size of objects"""
    size = sys.getsizeof(obj)
    if seen is None:
        seen = set()
    obj_id = id(obj)
    if obj_id in seen:
        return 0
    # Important mark as seen *before* entering recursion to gracefully handle
    # self-referential objects
    seen.add(obj_id)
    if isinstance(obj, dict):
        size += sum([get_size(v, seen) for v in obj.values()])
        size += sum([get_size(k, seen) for k in obj.keys()])
    elif hasattr(obj, '__dict__'):
        size += get_size(obj.__dict__, seen)
    elif hasattr(obj, '__iter__') and not isinstance(
        obj, (str, bytes, bytearray)
    ):
        size += sum([get_size(i, seen) for i in obj])
    return size


def append_eos_to_multiple_semantic_regions(
    formatted_data,
    data_ranges,
    eos_token,
    image_token,
    is_chat_data,
):

    if data_ranges == [] or not eos_token:
        return data_ranges
    eos_indices = []
    start_search_index = data_ranges[0].get("indices")[0]
    while start_search_index < len(formatted_data):
        eos_start_idx = formatted_data.find(eos_token, start_search_index)
        if eos_start_idx == -1:
            ## No eos found. Break
            break
        eos_end_idx = eos_start_idx + len(eos_token)
        start_search_index = eos_end_idx
        eos_indices.append((eos_start_idx, eos_end_idx))

    current_eos_pos = 0
    current_data_range_pos = 0
    while current_eos_pos < len(eos_indices) and current_data_range_pos < len(
        data_ranges
    ):
        eos_start_idx, eos_end_idx = eos_indices[current_eos_pos]
        region_start_idx, region_end_idx = data_ranges[
            current_data_range_pos
        ].get("indices")
        ## EOS occurs in the current region
        if region_start_idx <= eos_start_idx < region_end_idx:
            current_eos_pos += 1
            continue

        if current_data_range_pos + 1 < len(data_ranges):
            next_region_start_idx, next_region_end_idx = data_ranges[
                current_data_range_pos + 1
            ].get("indices")
            ## Check if eos occurs between current and next region
            if region_end_idx <= eos_start_idx < next_region_start_idx:
                image_start_idx = (
                    -1
                    if image_token is None
                    else formatted_data[region_end_idx:eos_start_idx].find(
                        image_token
                    )
                )
                if image_start_idx == -1:
                    indices_incl_eos = (region_start_idx, eos_end_idx)
                    data_ranges[current_data_range_pos][
                        "indices"
                    ] = indices_incl_eos
                    current_eos_pos += 1
        else:
            ## insert EOS in the last region
            image_start_idx = (
                -1
                if image_token is None
                else formatted_data[region_end_idx:eos_start_idx].find(
                    image_token
                )
            )
            if image_start_idx == -1:
                indices_incl_eos = (region_start_idx, eos_end_idx)
                data_ranges[current_data_range_pos][
                    "indices"
                ] = indices_incl_eos
                current_eos_pos += 1
        current_data_range_pos += 1

    if (
        not is_chat_data or len(eos_indices) > 1
    ):  ## 1 because the last eot could be eos
        return data_ranges

    for i in range(1, len(data_ranges)):
        start_idx, end_idx = data_ranges[i].get("indices")
        previous_start_idx, previous_end_idx = data_ranges[i - 1].get("indices")
        if previous_end_idx != start_idx:
            handle_turn_token = True
            data_ranges[i - 1]["handle_turn_token"] = True
        if i == len(data_ranges) - 1:
            if end_idx < len(formatted_data):
                data_ranges[i]["handle_turn_token"] = True

    return data_ranges


def find_region_in_formatted_string(text_semantic_region_list, formatted_data):

    string_search_idx = 0
    for semantic_region in text_semantic_region_list:
        region_identifier = semantic_region.pop("region_identifier", "")
        region_len = semantic_region.get("region_len")
        region_identifier_start_idx = formatted_data.find(
            region_identifier, string_search_idx
        )
        assert (
            region_identifier_start_idx != -1
        ), f"Unable to find region_identifier - {region_identifier} in the string - {formatted_data}"
        formatted_data = formatted_data.replace(region_identifier, "")
        start_idx = region_identifier_start_idx
        end_idx = start_idx + region_len
        string_search_idx = end_idx
        semantic_region.update({"indices": (start_idx, end_idx)})

    return formatted_data, text_semantic_region_list


def find_token_range(region, offsets, starting_offset_position):

    string_start, string_end = region.pop('indices')
    token_start = next(
        (
            i
            for i in range(starting_offset_position, len(offsets))
            if (offsets[i][0] <= string_start and offsets[i][1] > string_start)
            or (
                offsets[i][0] > string_start
            )  ## this condition is useful for neox tokenizer which treats space as an additional token
        ),
        None,
    )
    if token_start is None:
        raise ValueError(
            f"The implementation of offset mapping of this tokenizer may be incorrect. Check the huggingface implementation for more details."
        )
    token_end = next(
        (
            i
            for i in range(starting_offset_position, len(offsets))
            if offsets[i][1] >= string_end and offsets[i][0] < string_end
        ),
        None,
    )
    if token_end is None:
        raise ValueError(
            f"The huggingface implementation of offset mapping of this tokenizer may be incorrect. Check the huggingface implementation for more details."
        )
    data = {
        "indices": (token_start, token_end + 1),
        "loss_weight": region.get("loss_weight"),
        "attention_mask": region.get("attention_mask"),
    }

    return data


def truncate_sequence(
    token_ids,
    tokenized_semantic_region_list,
    max_sequence_length,
    max_turn_length,
    prompt_truncation_mode,
):
    """
    Truncates token sequences to fit within a specified MSL, parameterized by max_turn_length.

    Args:
        token_ids (list): List of token IDs representing the entire sequence.
        tokenized_semantic_region_list (list): List of tokenized semantic regions.
        max_sequence_length (int): Maximum allowed length of the sequence after truncation.
        max_turn_length (int): Maximum length of any single segment that can be present, after truncation.
        prompt_truncation_mode (str): Mode of truncation for prompt/user part of chat. Can be 'keep_start' or 'keep_end'.

    Returns:
        tokenized_semantic_region_list (list): Returned with indices updated for region after truncation.
        list: The truncated sequence of token IDs that fits within the max_sequence_length constraint.
    """

    def update_semantic_regions(
        part_one_list,
        part_two_list,
        part_one_indices_to_remove,
        part_two_indices_to_remove,
    ):
        combined_list = part_one_list + part_two_list
        combined_list.sort(key=lambda x: x[2][0])

        combined_rem = part_one_indices_to_remove + part_two_indices_to_remove
        combined_rem_dict = OrderedDict()

        for element in combined_rem:
            key = (element[0], element[1])
            value = (element[2], element[3])
            combined_rem_dict[key] = value

        updated_ranges = []
        cumulative_shift = 0

        for index, part, (original_start, original_end) in combined_list:
            removed_item = combined_rem_dict.get((index, part))

            if removed_item is not None:
                mode, (removed_start, removed_end) = removed_item
                current_shift = removed_end - removed_start

                if mode == "keep_start":
                    new_start, new_end = (
                        original_start - cumulative_shift,
                        removed_start - cumulative_shift,
                    )
                elif mode == "keep_end":
                    new_start, new_end = (
                        removed_end - cumulative_shift - current_shift,
                        original_end - cumulative_shift - current_shift,
                    )

                cumulative_shift += current_shift
            else:
                current_shift = 0
                new_start, new_end = (
                    original_start - cumulative_shift,
                    original_end - cumulative_shift,
                )
                cumulative_shift += current_shift

            updated_ranges.append((new_start, new_end))

        no_of_regions = 0
        for region in tokenized_semantic_region_list:
            no_of_regions += 1

        assert (
            len(updated_ranges) == no_of_regions
        ), "Mismatch in number of regions of tokenized_semantic_region_list and the updated ranges."

        index = 0
        for region in tokenized_semantic_region_list:
            region['indices'] = updated_ranges[index]
            index += 1

        return tokenized_semantic_region_list

    def _truncate(
        tokenized_semantic_region_list,
        part_one_list,
        part_two_list,
        truncate_length,
    ):
        """
        Helper function to truncate two parts of the sequence based on the provided length.

        Args:
            tokenized_semantic_region_list (list): List of semantic regions that are present.
            part_one_list (list): List of (start, end) tuples for the first part of the sequence.
            part_two_list (list): List of (start, end) tuples for the second part of the sequence.
            truncate_length (int): Total length that needs to be truncated from the sequence.

        Returns:
            list: Truncated sequence of token IDs.
        """

        # Enumerating the lists, to maintain indices (which are used later).
        part_one_list = list(enumerate(part_one_list))
        part_one_list = [
            (item[0], 'part_one', item[1]) for item in part_one_list
        ]

        part_two_list = list(enumerate(part_two_list))
        part_two_list = [
            (item[0], 'part_two', item[1]) for item in part_two_list
        ]

        part_one_indices_to_remove = []

        # Sort the ordered list by maximum turn length, with the maximum length indices coming first.
        sorted_part_one = sorted(
            part_one_list, key=lambda x: x[2][1] - x[2][0], reverse=True
        )

        # Truncate from the first part of the sequence.
        for index, part, (start, end) in sorted_part_one:
            length_of_turn = end - start

            """
                We also have to always maintain (max_turn_length) in every turn, after truncation.
                Therefore, the max amount that can be truncated = (length_of_turn - max_turn_length)

                What happens if length of turn is < max_turn_length?
                Then we keep the entire turn, and move to the next user and try truncating from there.
            """

            if max_turn_length >= length_of_turn:
                # Keep the entire turn; no truncation at all.
                continue
            else:
                # max_turn_length < length_of_turn i.e truncation is possible from this turn.
                available_truncate = length_of_turn - max_turn_length

                if available_truncate < truncate_length:
                    # Truncate the max you can, move to the next turn.
                    truncate_length -= available_truncate

                    if prompt_truncation_mode == "keep_start":
                        part_one_indices_to_remove.append(
                            (
                                index,
                                part,
                                'keep_start',
                                (end - available_truncate, end),
                            )
                        )
                    elif prompt_truncation_mode == "keep_end":
                        part_one_indices_to_remove.append(
                            (
                                index,
                                part,
                                'keep_end',
                                (start, start + available_truncate),
                            )
                        )
                else:
                    # Here, available_truncate >= truncate_length i.e we have more than what we need.
                    # Therefore, we'll take only what we need, and we have finished truncation from Part 1 solely.
                    if prompt_truncation_mode == "keep_start":
                        part_one_indices_to_remove.append(
                            (
                                index,
                                part,
                                'keep_start',
                                (end - truncate_length, end),
                            )
                        )
                    elif prompt_truncation_mode == "keep_end":
                        part_one_indices_to_remove.append(
                            (
                                index,
                                part,
                                'keep_end',
                                (start, start + truncate_length),
                            )
                        )

                    # Sorting this, in order to not mess up the indices while removing.
                    range_of_indices_to_remove_part_one = sorted(
                        part_one_indices_to_remove,
                        key=lambda x: x[3][0],
                        reverse=True,
                    )

                    for (
                        index,
                        part,
                        mode,
                        (start, end),
                    ) in range_of_indices_to_remove_part_one:
                        del token_ids[start:end]

                    assert (
                        len(token_ids) == max_sequence_length
                    ), "After truncation, the length of token IDs should be equal to MSL."

                    # Now, update tokenized_semantic_region_list.
                    tokenized_semantic_region_list = update_semantic_regions(
                        part_one_list,
                        part_two_list,
                        part_one_indices_to_remove,
                        [],
                    )

                    return tokenized_semantic_region_list, token_ids

        assert (
            truncate_length > 0
        ), "Truncation from second part should only happen if truncation from the first part is exhausted."

        # Calculate the total possible truncation length from the second part.
        total_possible_truncation = 0
        for index, part, (start, end) in part_two_list:
            total_possible_truncation += (end - start) - max_turn_length

        if total_possible_truncation < truncate_length:
            return (
                tokenized_semantic_region_list,
                {},
            )  # If the total truncation possible is not enough to meet the truncation length.
        else:
            part_two_indices_to_remove = []

            # Sorting this by max turn length, so that most of the truncation happens from the longest range.
            sorted_part_two = sorted(
                part_two_list, key=lambda x: x[2][1] - x[2][0], reverse=True
            )

            for index, part, (start, end) in sorted_part_two:
                length_of_turn = end - start

                if max_turn_length >= length_of_turn:
                    # Keep the entire turn; no truncation.
                    continue
                else:
                    # Truncate the maximum you can, move to the next turn. By default, we keep the end i.e "keep_start" for completion.
                    # This is done to maintain recent context as much as possible.
                    available_truncate = length_of_turn - max_turn_length

                    if available_truncate < truncate_length:
                        # We need to truncate more than what is availabe; thus truncate max you can and move to next turn.
                        truncate_length -= available_truncate
                        part_two_indices_to_remove.append(
                            (
                                index,
                                part,
                                'keep_start',
                                (end - available_truncate, end),
                            )
                        )
                    else:
                        # We can finish the truncation here, as what we have is more than what we need.
                        part_two_indices_to_remove.append(
                            (
                                index,
                                part,
                                'keep_start',
                                (end - truncate_length, end),
                            )
                        )
                        break

        # Sorting the indices in descending order, to maintain correctness while deleting.
        range_of_indices_to_remove = (
            part_one_indices_to_remove + part_two_indices_to_remove
        )
        range_of_indices_to_remove.sort(key=lambda x: x[3][0], reverse=True)

        for index, part, mode, (start, end) in range_of_indices_to_remove:
            del token_ids[start:end]

        assert (
            len(token_ids) == max_sequence_length
        ), "After truncation, the length of token IDs should be equal to MSL."

        tokenized_semantic_region_list = update_semantic_regions(
            part_one_list,
            part_two_list,
            part_one_indices_to_remove,
            part_two_indices_to_remove,
        )

        return tokenized_semantic_region_list, token_ids

    def _get_truncation_indices(tokenized_semantic_region_list):
        truncation_indices = {}
        for regions in tokenized_semantic_region_list:
            if regions['role'] not in truncation_indices:
                truncation_indices[regions['role']] = []

            truncation_indices[regions['role']].append(regions['indices'])
        return truncation_indices

    if prompt_truncation_mode not in ['keep_start', 'keep_end']:
        raise ValueError(
            "prompt_truncation_mode should only be 'keep_start' or 'keep_end'."
        )

    # Generate truncation indices
    truncation_indices = _get_truncation_indices(tokenized_semantic_region_list)

    # Determine which keys are present in the truncation indices dictionary.
    keys = set(truncation_indices.keys())

    # Total length to truncate.
    truncate_length = len(token_ids) - max_sequence_length

    if "prompt" in keys and "completion" in keys:
        # Adjusting for BOS token in prompt/completion.
        if truncation_indices['prompt'][0][0] != 0:
            truncation_indices['prompt'][0][0] = 0

        interaction_type = "prompt_completion"
        return _truncate(
            tokenized_semantic_region_list,
            truncation_indices['prompt'],
            truncation_indices['completion'],
            truncate_length,
        )
    elif "user" in keys and "assistant" in keys:
        interaction_type = "user_assistant"
        return _truncate(
            tokenized_semantic_region_list,
            truncation_indices['user'],
            truncation_indices['assistant'],
            truncate_length,
        )
    else:
        raise ValueError(
            "Truncation is only supported for 'prompt'/'completion' or 'user'/'assistant'."
        )
