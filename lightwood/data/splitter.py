from lightwood.api.dtype import dtype
import pandas as pd
import numpy as np
from typing import List, Dict
from itertools import product
from lightwood.api.types import TimeseriesSettings
from lightwood.helpers.log import log


def splitter(
    data: pd.DataFrame,
    tss: TimeseriesSettings,
    dtype_dict: Dict[str, str],
    seed: int,
    pct_train: float,
    pct_dev: float,
    pct_test: float,
    target: str
) -> Dict[str, pd.DataFrame]:
    """
    Splits data into training, dev and testing datasets. 
    Rows in the dataset are shuffled randomly. If a target value is provided and is of data type categorical/binary, then train/test/dev will be stratified to maintain the representative populations of each class.

    :param data: Input dataset to be split
    :param tss: time-series specific details for splitting
    :param dtype_dict: Dictionary with the data type of all columns
    :param seed: Random state for pandas data-frame shuffling
    :param pct_train: training fraction of data; must be less than 1
    :param pct_dev: dev fraction of data; must be less than 1
    :param pct_test: testing fraction of data; must be less than 1
    :param target: Name of the target column; if specified, data will be stratified on this column

    :returns: A dictionary containing the keys train, test and dev with their respective data frames, as well as the "stratified_on" key indicating which columns the data was stratified on (None if it wasn't stratified on anything)
    """ # noqa
    if sum(pct_train + pct_dev + pct_test) != 1:
        raise Exception('The train, dev and test percentage of the data needs to sum up to 1')

    # Shuffle the data
    np.random.seed(seed)
    if not tss.is_timeseries:
        data = data.sample(frac=1, random_state=seed).reset_index(drop=True)

    # Split the data
    train_cutoff = round(data.shape[0] * pct_train)
    dev_cutoff = train_cutoff + round(data.shape[0] * pct_dev)

    train = data[:train_cutoff]
    dev = data[train_cutoff:dev_cutoff]
    test = data[dev_cutoff:]

    # Perform stratification if specified
    stratify_on = []
    if target is not None:
        pcts = (pct_train, pct_dev, pct_test)
        train, dev, test, stratify_on = stratify_wrapper(train, dev, test, target, pcts, dtype_dict, tss)

    return {"train": train, "test": test, "dev": dev, "stratified_on": stratify_on}


def stratify_wrapper(train: pd.DataFrame,
                     dev: pd.DataFrame,
                     test: pd.DataFrame,
                     target: str,
                     pcts: (float, float, float),
                     dtype_dict: Dict[str, str],
                     tss: TimeseriesSettings) -> (pd.DataFrame, pd.DataFrame, pd.DataFrame, list):
    """
    Simple wrapper that acts as bridge between `splitter` and the actual `stratify` method.

    :param train: train dataset
    :param dev: dev dataset
    :param test: test dataset
    :param target: Name of the target column; if specified, data will be stratified on this column
    :param pcts: tuple with (train, dev, test) fractions of the data
    :param dtype_dict: Dictionary with the data type of all columns
    :param tss: time-series specific details for splitting
    """
    stratify_on = []
    if dtype_dict[target] in (dtype.categorical, dtype.binary):
        stratify_on += [target]
    if tss.is_timeseries and isinstance(tss.group_by, list):
        stratify_on += tss.group_by

    if stratify_on:
        pct_train, pct_dev, pct_test = pcts
        data = pd.concat([train, dev, test])
        gcd = np.gcd(100, np.gcd(pct_test, np.gcd(pct_train, pct_dev)))
        nr_subsets = int(100 / gcd)

        subsets = stratify(data, nr_subsets, stratify_on)
        subsets = randomize_uneven_stratification(data, subsets, nr_subsets, tss)

        train = pd.concat(subsets[0:int(pct_train / gcd)])
        dev = pd.concat(subsets[int(pct_train / gcd):int(pct_train / gcd + pct_dev / gcd)])
        test = pd.concat(subsets[int(pct_train / gcd + pct_dev / gcd):])

    return train, dev, test, stratify_on


def stratify(data: pd.DataFrame, nr_subset: int, stratify_on: List[str], random_alloc=False) -> List[pd.DataFrame]:
    """
    Stratified data splitter.
    
    The `stratify_on` columns yield a cartesian product by which every different subset will be stratified 
    independently from the others, and recombined at the end. 
    
    For grouped time series tasks, each group yields a different time series. That is, the splitter generates
    `nr_subsets` subsets from `data`, with equally-sized sub-series for each group.

    :param data: Data to be split
    :param nr_subset: Number of subsets to create
    :param stratify_on: Columns to group-by on
    :param random_alloc: Whether to allocate subsets randomly

    :returns A list of equally-sized data subsets that can be concatenated by the full data. This preserves the group-by columns.
    """  # noqa
    # TODO: Make stratification work for regression via histogram bins??
    all_group_combinations = list(product(*[data[col].unique() for col in stratify_on]))

    subsets = [pd.DataFrame() for _ in range(nr_subset)]
    for group in all_group_combinations:
        subframe = data
        for idx, col in enumerate(stratify_on):
            subframe = subframe[subframe[col] == group[idx]]

        subset = np.array_split(subframe, nr_subset)

        # Allocate to subsets randomly
        if random_alloc:
            already_visited = []
            for n in range(nr_subset):
                i = np.random.randint(nr_subset)
                while i in already_visited:
                    i = np.random.randint(nr_subset)
                already_visited.append(i)
                subsets[n] = pd.concat([subsets[n], subset[i]])
        else:
            for n in range(nr_subset):
                subsets[n] = pd.concat([subsets[n], subset[n]])

    return subsets


def randomize_uneven_stratification(data: pd.DataFrame, subsets: List[pd.DataFrame], nr_subsets: int,
                                    tss: TimeseriesSettings, len_threshold: int = 2):
    """
    Helper function reverts stratified data back to a normal split if the size difference between splits is larger
    than a certain threshold.

    :param data: Raw data
    :param subsets: Stratified data
    :param nr_subsets: Number of subsets
    :param tss: TimeseriesSettings
    :param len_threshold: size difference between subsets to revert the stratification process

    :return: Inplace-modified subsets if threshold was passed. Else, subsets are returned unmodified.
    """
    if not tss.is_timeseries:
        max_len = np.max([len(subset) for subset in subsets])
        for subset in subsets:
            if len(subset) < max_len - len_threshold:
                subset_lengths = [len(subset) for subset in subsets]
                log.warning(f'Cannot stratify, got subsets of length: {subset_lengths} | Splitting without stratification')  # noqa
                subsets = np.array_split(data, nr_subsets)
                break
    return subsets
