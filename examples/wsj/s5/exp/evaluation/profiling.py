#! /usr/bin/env python
# -*- coding: utf-8 -*-

"""Show the results of profiling (WSJ corpus)."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from os.path import join
import argparse
import pstats

parser = argparse.ArgumentParser()
parser.add_argument('--model_path', type=str,
                    help='path to the model to evaluate')


def main():

    args = parser.parse_args()

    stats = pstats.Stats(join(args.model_path, 'train.profile'))
    stats.sort_stats('time').print_stats(100)


if __name__ == '__main__':
    main()
