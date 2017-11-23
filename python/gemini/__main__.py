import argparse
import json
import logging
import sys
from time import time

from modelforge.logs import setup_logging
from sourced.ml.repo2 import wmhash

from gemini.bags import source2bags
from gemini.cassandra_utils import reset_db
from gemini.graph import print_hash_graph
from gemini.hasher import hash_batches
from gemini.query import query
from gemini.warmup import warmup


CASSANDRA_PACKAGE = "com.datastax.spark:spark-cassandra-connector_2.11:2.0.3"


def get_parser() -> argparse.ArgumentParser:
    """
    Create main parser.

    :return: Parser
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--log-level", default="INFO",
                        choices=logging._nameToLevel,
                        help="Logging verbosity.")

    def add_spark_args(my_parser):
        my_parser.add_argument(
            "-s", "--spark", default="local[*]", help="Spark's master address.")
        my_parser.add_argument(
            "--config", nargs="+", default=[], help="Spark configuration (key=value).")
        my_parser.add_argument(
            "--package", nargs="+", default=[CASSANDRA_PACKAGE], help="Additional Spark package.")
        my_parser.add_argument(
            "--spark-local-dir", default="/tmp/spark", help="Spark local directory.")

    def add_engine_args(my_parser):
        add_spark_args(my_parser)
        my_parser.add_argument(
            "--bblfsh", default="localhost", help="Babelfish server's address.")
        my_parser.add_argument(
            "--engine", default="0.1.8", help="source{d} engine version.")

    def add_features_arg(my_parser, required: bool, suffix="."):
        my_parser.add_argument(
            "-f", "--feature", nargs="+",
            choices=[ex.NAME for ex in wmhash.__extractors__.values()],
            required=required, help="The feature extraction scheme to apply" + suffix)
        for ex in wmhash.__extractors__.values():
            for opt, val in ex.OPTS.items():
                my_parser.add_argument("--%s-%s" % (ex.NAME, opt), default=val, type=json.loads,
                                       help="%s's kwarg" % ex.__name__)

    def add_cassandra_args(my_parser):
        my_parser.add_argument(
            "--cassandra", default="0.0.0.0:9042", help="Cassandra's host:port.")
        my_parser.add_argument("--keyspace", default="gemini",
                               help="Cassandra's key space.")
        my_parser.add_argument(
            "--tables", help="Table name mapping (JSON): bags, hashes, hashtables, hashtables2.")

    def add_wmh_args(my_parser, params_help: str, add_hash_size: bool, required: bool):
        if add_hash_size:
            my_parser.add_argument("--size", type=int, default=128, help="Hash size.")
        my_parser.add_argument("-p", "--params", required=required, help=params_help)
        my_parser.add_argument("-t", "--threshold", required=required, type=float,
                               help="Jaccard similarity threshold.")
        my_parser.add_argument("--false-positive-weight", type=float, default=0.5,
                               help="Used to adjust the relative importance of "
                                    "minimizing false positives count when optimizing "
                                    "for the Jaccard similarity threshold.")
        my_parser.add_argument("--false-negative-weight", type=float, default=0.5,
                               help="Used to adjust the relative importance of "
                                    "minimizing false negatives count when optimizing "
                                    "for the Jaccard similarity threshold.")

    subparsers = parser.add_subparsers(help="Commands", dest="command")
    source2bags_parser = subparsers.add_parser(
        "bags", help="Convert source code to weighted sets.")
    source2bags_parser.set_defaults(handler=source2bags)
    source2bags_parser.add_argument(
        "-r", "--repositories", required=True,
        help="The path to the repositories.")
    source2bags_parser.add_argument(
        "--batches", required=True,
        help="[OUT] The path to the Parquet files with bag batches.")
    source2bags_parser.add_argument(
        "--docfreq", required=True,
        help="[OUT] The path to the OrderedDocumentFrequencies model.")
    source2bags_parser.add_argument(
        "--vocabulary-size", default=10000000, type=int,
        help="The maximum vocabulary size.")
    source2bags_parser.add_argument(
        "--min-docfreq", default=1, type=int,
        help="The minimum document frequency of each element.")
    add_features_arg(source2bags_parser, True)
    source2bags_parser.add_argument(
        "-l", "--language", choices=("Java", "Python"),
        help="The programming language to analyse.")
    source2bags_parser.add_argument(
        "--persist", default=None, help="Persistence type (StorageClass.*).")
    source2bags_parser.add_argument(
        "--graph", help="Write the tree in Graphviz format to this file.")
    add_cassandra_args(source2bags_parser)
    add_engine_args(source2bags_parser)

    warmup_parser = subparsers.add_parser(
        "warmup", help="Initialize source{d} engine.")
    warmup_parser.set_defaults(handler=warmup)
    add_engine_args(warmup_parser)

    hash_parser = subparsers.add_parser(
        "hash", help="Run MinHashCUDA on the bag batches.")
    hash_parser.set_defaults(handler=hash_batches)
    hash_parser.add_argument("input",
                             help="Path to the directory with Parquet files.")
    hash_parser.add_argument("--seed", type=int, default=int(time()),
                             help="Random generator's seed.")
    hash_parser.add_argument("--mhc-verbosity", type=int, default=1,
                             help="MinHashCUDA logs verbosity level.")
    hash_parser.add_argument("--devices", type=int, default=0,
                             help="Or-red indices of NVIDIA devices to use. 0 means all.")
    add_wmh_args(hash_parser, "Path to the output file with WMH parameters.", True, True)
    add_cassandra_args(hash_parser)
    add_spark_args(hash_parser)

    query_parser = subparsers.add_parser("query", help="Query for similar files.")
    query_parser.set_defaults(handler=query)
    mode_group = query_parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument("-i", "--id", help="Query for this id (id mode).")
    mode_group.add_argument("-c", "--file", help="Query for this file (file mode).")
    query_parser.add_argument("--docfreq", help="Path to OrderedDocumentFrequencies (file mode).")
    query_parser.add_argument(
        "--bblfsh", default="localhost:9432", help="Babelfish server's endpoint.")
    add_features_arg(query_parser, False, " (file mode).")
    query_parser.add_argument("-x", "--precise", action="store_true",
                              help="Calculate the precise set.")
    query_parser.add_argument("-o", "--format", choices=("human", "json"), help="Output format.")
    add_wmh_args(query_parser, "Path to the Weighted MinHash parameters.", False, False)
    add_cassandra_args(query_parser)

    db_parser = subparsers.add_parser("resetdb", help="Destructively initialize the database.")
    db_parser.set_defaults(handler=reset_db)
    add_cassandra_args(db_parser)
    db_parser.add_argument(
        "--hashes-only", action="store_true",
        help="Only clear the tables: hashes, hashtables, hashtables2. Do not touch the rest.")

    hashgraph_parser = subparsers.add_parser(
        "hashgraph", help="Print all similar pairs of files according to Weighted MinHash. "
                          "Needs | sort | uniq to eliminate duplicates.")
    hashgraph_parser.set_defaults(handler=print_hash_graph)
    add_cassandra_args(hashgraph_parser)

    # TODO: retable [.....] -> [.] [.] [.] [.] [.]

    return parser


def main():
    """
    Creates all the argparse-rs and invokes the function from set_defaults().

    :return: The result of the function from set_defaults().
    """
    parser = get_parser()
    args = parser.parse_args()
    args.log_level = logging._nameToLevel[args.log_level]
    setup_logging(args.log_level)
    try:
        handler = args.handler
    except AttributeError:
        def print_usage(_):
            parser.print_usage()

        handler = print_usage
    return handler(args)

if __name__ == "__main__":
    sys.exit(main())
