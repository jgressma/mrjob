# Copyright 2019 Yelp
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""A Spark script that can run a MRJob without Hadoop."""
import sys
from argparse import ArgumentParser
from importlib import import_module

from mrjob.util import shlex_split

# switches which mr_spark_harness.py passes through to the harness
#
# these are tuples of (args, kwargs) for ArgumentParser.add_argument()
_PASSTHRU_OPTIONS = [
    (['--job-args'], dict(
        default=None,
        dest='job_args',
        help=('The arguments pass to the MRJob. Please quote all passthru args'
              ' so that they are in the same string'),
    )),
    (['--start-step'], dict(
        default=None,
        dest='start_step',
        type=int,
        help=("Don't run steps before the step with this (0-indexed) step"
              " number. Use with --end to define a range of steps to run")
    )),
    (['--end-step'], dict(
        default=None,
        dest='end_step',
        type=int,
        help=("Don't run the step with this (0-indexed) step number, or steps"
              " after it. Use with --start to define a range of steps to run")
    )),
    (['--compression-codec'], dict(
        default=None,
        dest='compression_codec',
        help=('Java class path of a codec to use to compress output.'),
    )),
]


def main(cmd_line_args=None):
    if cmd_line_args is None:
        cmd_line_args = sys.argv[1:]

    parser = _make_arg_parser()
    args = parser.parse_args(cmd_line_args)

    # get job_class
    job_module_name, job_class_name = args.job_class.rsplit('.', 1)
    job_module = import_module(job_module_name)
    job_class = getattr(job_module, job_class_name)

    if args.job_args:
        job_args = shlex_split(args.job_args)
    else:
        job_args = []

    def make_job(*args):
        j = job_class(job_args + list(args))
        j.sandbox()  # so Spark doesn't try to serialize stdin
        return j

    # load initial data
    from pyspark import SparkContext
    sc = SparkContext()
    rdd = sc.textFile(args.input_path, use_unicode=False)

    # get job steps. don't pass --steps, which is deprecated
    steps = make_job().steps()

    # process steps
    steps_to_run = list(enumerate(steps))[args.start_step:args.end_step]

    for step_num, step in steps_to_run:
        rdd = _run_step(step, step_num, rdd, make_job)

    # write the results
    rdd.saveAsTextFile(
        args.output_path, compressionCodecClass=args.compression_codec)


def _run_step(step, step_num, rdd, make_job):
    """Run the given step on the RDD and return the transformed RDD."""
    step_desc = step.description(step_num)
    _check_step(step_desc, step_num)

    # creating a separate job instances to ensure that initialization
    # happens correctly (e.g. mapper_job.is_task() should be True).
    # probably doesn't actually matter that we pass --step-num
    mapper_job, reducer_job, combiner_job = (
        make_job('--%s' % mrc, '--step-num=%d' % step_num)
        if step_desc.get(mrc) else None
        for mrc in ('mapper', 'reducer', 'combiner')
    )

    if mapper_job is not None:
        m_read, m_write = mapper_job.pick_protocols(step_num, 'mapper')

        # run the mapper
        rdd = rdd.map(m_read)
        rdd = rdd.mapPartitions(
            lambda pairs: mapper_job.map_pairs(pairs, step_num))
        rdd = rdd.map(lambda k_v: m_write(*k_v))

    if combiner_job is not None:
        c_read, c_write = combiner_job.pick_protocols(step_num, 'combiner')

        # run the combiner
        rdd = rdd.map(c_read)

        # Most combiners should return a single value per key to shrink the
        # mapper output. If the combiner does not seem to be doing this, fall
        # back to recreating the mapper output since Hadoop combiners are
        # optional anyway and we do not want to run the same values through the
        # combiner multiple times.
        def combiner_helper(pairs1, pairs2):
            if len(pairs1) == len(pairs2) == 1:
                return list(
                    combiner_job.combine_pairs(pairs1 + pairs2, step_num),
                )
            else:
                pairs1.extend(pairs2)
                return pairs1

        # combine_pairs takes a list of key-value pairs; restructuring the
        # data so that we can provide that
        rdd = rdd.map(lambda k_v: (k_v[0], k_v))

        # a shuffle is unneccesary in this case since combineByKey returns an
        # RDD grouped by key.
        rdd = rdd.combineByKey(
            createCombiner=lambda k_v: [k_v],
            mergeValue=lambda k_v_list, k_v: combiner_helper(k_v_list, [k_v]),
            mergeCombiners=combiner_helper,
        )
        rdd = rdd.mapValues(
            lambda pairs: [c_write(*pair) for pair in pairs])
        rdd = _flatten_and_maybe_sort_keys(
            rdd,
            should_sort=(
                reducer_job.sort_values() if reducer_job is not None else False
            ))
    elif reducer_job is not None and combiner_job is None:
        # simulate shuffle in Hadoop Streaming
        rdd = rdd.groupBy(lambda line: line.split(b'\t')[0])
        rdd = _flatten_and_maybe_sort_keys(
            rdd, should_sort=reducer_job.sort_values())

    if reducer_job is not None:
        r_read, r_write = reducer_job.pick_protocols(step_num, 'reducer')

        # run the reducer
        rdd = rdd.map(r_read)
        rdd = rdd.mapPartitions(
            lambda pairs: reducer_job.reduce_pairs(pairs, step_num))
        rdd = rdd.map(lambda k_v: r_write(*k_v))

    return rdd


def _flatten_and_maybe_sort_keys(rdd, should_sort):
    if should_sort:
        return rdd.flatMap(lambda key_and_lines: sorted(key_and_lines[1]))
    else:
        return rdd.flatMap(lambda key_and_lines: key_and_lines[1])


def _check_step(step_desc, step_num):
    """Check that the given step description is for a MRStep
    with no input manifest"""
    if step_desc.get('type') != 'streaming':
        raise ValueError(
            'step %d has unexpected type: %r' % (
                step_num, step_desc.get('type')))

    if step_desc.get('input_manifest'):
        raise NotImplementedError(
            'step %d uses an input manifest, which is unsupported')

    for mrc in ('mapper', 'reducer'):
        # bad combiners won't cause an error because they're optional
        substep_desc = step_desc.get(mrc)
        if not substep_desc:
            continue

        if substep_desc.get('type') != 'script':
            raise NotImplementedError(
                "step %d's %s has unexpected type: %r" % (
                    step_num, mrc, substep_desc.get('type')))

        if substep_desc.get('pre_filter'):
            raise NotImplementedError(
                "step %d's %s has pre-filter, which is unsupported" % (
                    step_num, mrc))


def _make_arg_parser():
    parser = ArgumentParser()

    parser.add_argument(
        dest='job_class',
        help=('dot-separated module and name of MRJob class. For example:'
              ' mrjob.examples.mr_wc.MRWordCountUtility'))

    parser.add_argument(
        dest='input_path',
        help=('Where to read input from. Can be a path or a URI, or several of'
              ' these joined by commas'))

    parser.add_argument(
        dest='output_path',
        help=('An empty directory to write output to. Can be a path or URI.'))

    for args, kwargs in _PASSTHRU_OPTIONS:
        parser.add_argument(*args, **kwargs)

    return parser


if __name__ == '__main__':
    main()
