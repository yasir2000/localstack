import json
import logging
import os
import re
from datetime import datetime
from typing import Dict, List, Optional, Pattern, Set, Tuple

import pytest
from _pytest.fixtures import SubRequest
from _pytest.nodes import Item
from _pytest.reports import TestReport
from _pytest.runner import CallInfo
from botocore.response import StreamingBody
from deepdiff import DeepDiff
from pluggy.callers import _Result

LOG = logging.getLogger(__name__)

PATTERN_ARN = re.compile(
    r"arn:(aws[a-zA-Z-]*)?:([a-zA-Z0-9-_.]+)?:([a-z]{2}(-gov)?-[a-z]+-\d{1})?:(\d{12})?(:[^:\\\"]+)+"
)
PATTERN_UUID = re.compile(
    r"[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}"
)
PATTERN_ISO8601 = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}(\+[0-9]{4})?"
)
PATTERN_S3_URL = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}(\+[0-9]{4})?"
)
PATTERN_SQS_URL = re.compile(
    r"https?://[^/]+/\d{12}/[^/\"]+"
)  # TODO: differences here between AWS + localstack structure


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: Item, call: CallInfo[None]) -> Optional[TestReport]:
    result: _Result = yield
    report: TestReport = result.result

    if call.excinfo is not None and isinstance(call.excinfo.value, SnapshotAssertionError):
        err: SnapshotAssertionError = call.excinfo.value
        report.longrepr = json.dumps(json.loads(err.result.result.to_json()), indent=2)
    return report


@pytest.fixture(name="snapshot", scope="function")
def fixture_snapshot(request: SubRequest, sts_client):

    sm = SnapshotManager(
        file_path=os.path.join(
            request.fspath.dirname, f"{request.fspath.purebasename}.snapshot.json"
        ),
        scope_key=request.node.nodeid,
        update=request.config.option.snapshot_update,
        verify=request.config.option.snapshot_verify,
    )
    # TODO: cache this
    account_id = sts_client.get_caller_identity()["Account"]
    sm.register_replacement(re.compile(account_id), "1" * 12)

    yield sm
    sm.persist_state()


class SnapshotMatchResult:
    def __init__(self, a: dict, b: dict):
        self.a = a
        self.b = b
        self.result = DeepDiff(a, b)

    def __bool__(self) -> bool:
        return not self.result

    def __repr__(self):
        return self.result.pretty()


class SnapshotAssertionError(AssertionError):
    def __init__(self, msg: str, result: SnapshotMatchResult):
        self.msg = msg
        self.result = result
        super(SnapshotAssertionError, self).__init__(msg)


class SnapshotManager:
    """
    snapshot handler for a single test function with potentially multiple assertions\
    Since it technically only  modifies a subset of the underlying snapshot file,
    it assumes that a single snapshot file is only being written to sequentially
    """

    results: List[SnapshotMatchResult]
    state: Dict[str, dict]
    called_keys: Set[str]

    replacers: List[Tuple[Pattern[str], str]]
    skip_keys: List[Tuple[Pattern[str], str]]

    def __init__(
        self, *, file_path: str, update: bool, scope_key: str, verify: Optional[bool] = False
    ):
        self.verify = verify
        self.update = update
        self.file_path = file_path
        self.scope_key = scope_key
        self.called_keys = set()
        self.replacers = []
        self.skip_keys = []
        self.results = []
        self.state = self.load_state()
        if scope_key not in self.state:
            self.state[scope_key] = {}

        # registering some defaults
        self.register_replacement(PATTERN_ARN, "<arn>")
        self.register_replacement(PATTERN_UUID, "<uuid>")
        self.register_replacement(PATTERN_ISO8601, "<date>")
        self.register_replacement(PATTERN_S3_URL, "<s3-url>")
        self.register_replacement(PATTERN_SQS_URL, "<sqs-url>")

        self.skip_key(re.compile(r"^.*Name$"), "<name>")
        self.skip_key(re.compile(r"^.*ResponseMetadata$"), "<response-metadata>")
        self.skip_key(re.compile(r"^.*Location$"), "<location>")
        self.skip_key(re.compile(r"^.*timestamp.*$", flags=re.IGNORECASE), "<timestamp>")
        self.skip_key(
            re.compile(r"^.*sha.*$", flags=re.IGNORECASE), "<sha>"
        )  # TODO: instead of skipping, make zip building reproducable

    def register_replacement(self, pattern: Pattern[str], value: str):
        self.replacers.append((pattern, value))

    def skip_key(self, pattern: Pattern[str], value: str):
        self.skip_keys.append((pattern, value))

    def persist_state(self) -> None:
        if self.update:
            with open(self.file_path, "w") as fd:
                try:
                    fd.write(json.dumps(self.state, indent=2))
                except Exception as e:
                    LOG.exception(e)

    def load_state(self) -> dict:
        try:
            with open(self.file_path, "r") as fd:
                content = fd.read()
                if content:
                    return json.loads(content)
                else:
                    return {}
        except FileNotFoundError:
            return {}

    def _update(self, key: str, obj_state: dict) -> None:
        self.state[self.scope_key][key] = obj_state

    def match(self, key: str, obj: dict) -> SnapshotMatchResult:
        __tracebackhide__ = True

        if key in self.called_keys:
            raise Exception(f"Key {key} used multiple times in the same test scope")
        self.called_keys.add(key)

        obj_state = self._transform(obj)

        if self.update:
            self._update(key, obj_state)
            return SnapshotMatchResult({}, {})

        sub_state = self.state[self.scope_key].get(key)
        if sub_state is None:
            raise Exception("Please run the test first with --snapshot-update")

        return SnapshotMatchResult(sub_state, obj_state)

    def assert_match(self, key: str, obj: dict) -> None:
        """
        Primary tester-facing interface. (Call this method in your test case.)
        Internally this raises an AssertionError and properly handles output formatting for the diff
        """
        __tracebackhide__ = True
        if not self.update and not self.verify:
            return

        result = self.match(key, obj)
        self.results.append(result)
        if not result and self.verify:
            raise SnapshotAssertionError("Parity snapshot failed", result=result)

    def _transform(self, old: dict) -> dict:
        """build a persistable state definition that can later be compared against"""

        new_dict = {}
        for k, v in old.items():

            skipped = False
            for (pattern, repl) in self.skip_keys:
                if pattern.match(k):
                    new_dict[k] = repl
                    skipped = True
                    continue
            if skipped:
                continue

            if isinstance(v, dict):
                new_dict[k] = self._transform(v)
            elif isinstance(v, list):
                # assumption: no nested lists in API calls
                new_list = []
                for i in v:
                    if isinstance(i, dict):
                        new_list.append(self._transform(i))
                    elif isinstance(i, str):
                        new_list.append(i)
                    else:  # assumption: has to be an int or boolean
                        new_list.append(v)
                new_dict[k] = new_list
            elif isinstance(v, str):
                new_dict[k] = v
            elif isinstance(v, StreamingBody):
                new_dict[k] = v.read().decode("utf-8")
            elif isinstance(v, datetime):  # TODO: remove when structural matching is implemented
                new_dict[k] = "<date>"
            else:
                new_dict[k] = v

        tmp_str = json.dumps(new_dict)
        for (pattern, repl) in self.replacers:
            tmp_str = re.sub(pattern, repl, tmp_str)

        return json.loads(tmp_str)
