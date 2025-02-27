import re
import sys
import fnmatch
import logging

from rma.redis import RmaRedis
from rma.scanner import Scanner
from rma.splitter import SimpleSplitter
from rma.redis_types import *
from rma.rule import *
from rma.reporters import *
from rma.helpers import floored_percentage

from collections import defaultdict
from redis.exceptions import ResponseError
from tqdm import tqdm

def ptransform(nm):
    if nm.startswith('celery-task-meta'):
        spl = nm.split('-')
        rt = '-'.join(spl[0:3])+':'+'-'.join(spl[3:])
    elif nm.startswith('qo_cli.aff_aggregations.aggregate_aff_aname_aname'):
        spl = nm.split('-')
        rt = '-'.join(spl[0:1])+':'+'-'.join(spl[1:])
    elif nm.endswith('_trigger_queue_user_job'):
        spl = nm.split('_')
        rt = '_'.join(spl[1:])+':'+'_'.join(spl[0:1])
    elif nm.endswith('.reply.celery.pidbox'):
        spl = nm.split('.')
        rt = '.'.join(spl[1:])+':'+spl[0]
    elif nm.endswith('_user_queue_user_job'):
        spl = nm.split('_')
        rt = '_'.join(spl[1:])+':'+spl[0]
    else:
        rt = nm
    return rt


def connect_to_redis(host, port, db=0, password=None, ssl=False):
    """

    :param host:
    :param port:
    :param db:
    :param password:
    :return RmaRedis:
    """
    try:
        redis = RmaRedis(host=host, port=port, db=db, password=password, ssl=ssl)
        if not check_redis_version(redis):
            sys.stderr.write('This script only works with Redis Server version 2.6.x or higher\n')
            sys.exit(-1)
    except ConnectionError as e:
        sys.stderr.write('Could not connect to Redis Server : %s\n' % e)
        sys.exit(-1)
    except ResponseError as e:
        sys.stderr.write('Could not connect to Redis Server : %s\n' % e)
        sys.exit(-1)
    return redis


def check_redis_version(redis):
    server_info = redis.info()
    version_str = server_info['redis_version']
    version = tuple(map(int, version_str.split('.')))

    if version[0] > 2 or (version[0] == 2 and version[1] >= 6):
        return True
    else:
        return False


class RmaApplication(object):
    globals = []

    types_rules = {
        REDIS_TYPE_ID_STRING: [],
        REDIS_TYPE_ID_HASH: [],
        REDIS_TYPE_ID_LIST: [],
        REDIS_TYPE_ID_SET: [],
        REDIS_TYPE_ID_ZSET: [],
    }

    def __init__(self, host="127.0.0.1", port=6367, password=None, db=0, ssl=False, match="*", limit=0, filters=None, logger=None, format="text", separator=":"):
        self.logger = logger or logging.getLogger(__name__)
        self.logger.info("start init Redis memory analyzer application")

        self.splitter = SimpleSplitter(separator)
        self.isTextFormat = format == "text"
        self.reporter = TextReporter() if self.isTextFormat else JsonReporter()
        self.redis = connect_to_redis(host=host, port=port, db=db, password=password, ssl=ssl)

        self.match = match
        self.limit = limit if limit != 0 else sys.maxsize

        if 'types' in filters:
            self.types = list(map(redis_type_to_id, filters['types']))
        else:
            self.types = REDIS_TYPE_ID_ALL

        if 'behaviour' in filters:
            self.behaviour = filters['behaviour']
        else:
            self.behaviour = 'all'

    def init_globals(self, redis):
        self.globals.append(GlobalKeySpace(redis=redis))

    def init_types_rules(self, redis):
        self.types_rules[REDIS_TYPE_ID_STRING].append(KeyString(redis=redis))
        self.types_rules[REDIS_TYPE_ID_STRING].append(ValueString(redis=redis))
        self.types_rules[REDIS_TYPE_ID_HASH].append(KeyString(redis=redis))
        self.types_rules[REDIS_TYPE_ID_HASH].append(Hash(redis=redis))
        self.types_rules[REDIS_TYPE_ID_LIST].append(KeyString(redis=redis))
        self.types_rules[REDIS_TYPE_ID_LIST].append(List(redis=redis))

        self.types_rules[REDIS_TYPE_ID_SET].append(KeyString(redis=redis))
        self.types_rules[REDIS_TYPE_ID_SET].append(Set(redis=redis))

        self.types_rules[REDIS_TYPE_ID_ZSET].append(KeyString(redis=redis))

    def run(self):
        self.init_types_rules(redis=self.redis)
        self.init_globals(redis=self.redis)

        str_res = []
        is_all = self.behaviour == 'all'
        with Scanner(redis=self.redis, match=self.match, accepted_types=self.types) as scanner:
            keys = defaultdict(list)
            for v in scanner.scan(limit=self.limit):
                keys[v["type"]].append(v)

            if self.isTextFormat:
                self.logger.info("Aggregating keys by pattern and type,%d", self.redis.dbsize())

            keys = {k: self.get_pattern_aggregated_data(v) for k, v in keys.items()}

            if self.isTextFormat:
                self.logger.info("Apply rules")

            if self.behaviour == 'global' or is_all:
                str_res.append(self.do_globals())
            if self.behaviour == 'scanner' or is_all:
                str_res.append(self.do_scanner(self.redis, keys))
            if self.behaviour == 'ram' or is_all:
                str_res.append(self.do_ram(keys))

        self.reporter.print(str_res)

    def do_globals(self):
        nodes = []
        for glob in self.globals:
            nodes.append(glob.analyze())

        return {"nodes": nodes}

    def do_scanner(self, r, res):
        keys = []
        total = min(r.dbsize(), self.limit)
        for key, aggregate_patterns in res.items():
            r_type = type_id_to_redis_type(key)
            self.logger.debug("do_scanner,%s,%d", r_type, len(aggregate_patterns))

            for k, v in aggregate_patterns.items():
                self.logger.debug("do_scanner item,%s,%s,%d", r_type, k, len(v))
                keys.append([k, len(v), r_type, floored_percentage(len(v) / total, 2)])
                keys.sort(key=lambda x: x[1], reverse=True)

        return {"keys": {"data": keys, "headers": ['name', 'count', 'type', 'percent']}}

    def do_ram(self, res):
        ret = {}

        for key, aggregate_patterns in res.items():
            if key in self.types_rules and key in self.types:
                redis_type = type_id_to_redis_type(key)
                self.logger.info("do_ram,%s,%d", redis_type, len(aggregate_patterns))
                for rule in self.types_rules[key]:
                    total_keys = sum(len(values) for key, values in aggregate_patterns.items())
                    self.logger.info("do_ram item,%s,%s,%d", redis_type, type(rule).__name__, total_keys)
                    ret[redis_type] = rule.analyze(keys=aggregate_patterns, total=total_keys)

        return {"stat": ret}

    def get_pattern_aggregated_data(self, data):
        """
        map redis key data to pattern.
        :param data: [{'name': 'a:b:c:0123', 'type': 1, 'encoding': 4, 'ttl': -1}]
        :return dict: { 'a:b:c:*': [ 'a:b:c:0123' ] }
        """
        redis_type = type_id_to_redis_type(data[0]['type'])
        self.logger.info("get_pattern_aggregated_data,%s,%d", redis_type, len(data))
        pattern = re.compile(r':US:[^:]+')
        split_patterns = self.splitter.split((ptransform(re.sub(pattern, ':US:1', obj["name"])) for obj in data))
        self.logger.info("split_patterns,%s,%d", redis_type, len(split_patterns))

        aggregate_patterns = {item: [] for item in split_patterns}

        with tqdm(total=len(split_patterns), desc="fnmatch {0}".format(redis_type)) as progress:
            for pattern in split_patterns:
                aggregate_patterns[pattern] = list(filter(lambda obj: fnmatch.fnmatch(ptransform(obj["name"]), pattern), data))
                progress.update()

        return aggregate_patterns
