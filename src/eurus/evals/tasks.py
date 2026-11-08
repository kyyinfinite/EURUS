from __future__ import annotations

import sys
from dataclasses import dataclass, field

CHECK = (sys.executable, "-m", "pytest", "-q", "--tb=short")


@dataclass(frozen=True, slots=True)
class EvalTask:
    name: str
    prompt: str
    files: dict[str, str]
    solution: dict[str, str]
    check: tuple[str, ...] = field(default=CHECK)


TASKS: tuple[EvalTask, ...] = (
    EvalTask(
        name="fix_off_by_one",
        prompt="The tests in test_window.py fail. Fix the bug in window.py.",
        files={
            "window.py": "def last_n(items, n):\n    return items[-n - 1:]\n",
            "test_window.py": (
                "from window import last_n\n\n\n"
                "def test_last_n():\n"
                "    assert last_n([1, 2, 3, 4, 5], 2) == [4, 5]\n\n\n"
                "def test_last_n_all():\n"
                "    assert last_n([1, 2], 5) == [1, 2]\n"
            ),
        },
        solution={"window.py": "def last_n(items, n):\n    return items[-n:] if n > 0 else []\n"},
    ),
    EvalTask(
        name="implement_slugify",
        prompt="Implement slugify in slug.py so that test_slug.py passes.",
        files={
            "slug.py": "def slugify(text):\n    raise NotImplementedError\n",
            "test_slug.py": (
                "from slug import slugify\n\n\n"
                "def test_basic():\n"
                "    assert slugify('Hello World') == 'hello-world'\n\n\n"
                "def test_punctuation_and_spaces():\n"
                "    assert slugify('  Hello,   World!  ') == 'hello-world'\n\n\n"
                "def test_empty():\n"
                "    assert slugify('!!!') == ''\n"
            ),
        },
        solution={
            "slug.py": (
                "import re\n\n\n"
                "def slugify(text):\n"
                "    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')\n"
            )
        },
    ),
    EvalTask(
        name="fix_mutable_default",
        prompt="test_tags.py fails when add_tag is called twice. Find and fix the cause.",
        files={
            "tags.py": ("def add_tag(tag, tags=[]):\n    tags.append(tag)\n    return tags\n"),
            "test_tags.py": (
                "from tags import add_tag\n\n\n"
                "def test_independent_calls():\n"
                "    assert add_tag('a') == ['a']\n"
                "    assert add_tag('b') == ['b']\n\n\n"
                "def test_explicit_list():\n"
                "    assert add_tag('c', ['x']) == ['x', 'c']\n"
            ),
        },
        solution={
            "tags.py": (
                "def add_tag(tag, tags=None):\n"
                "    tags = [] if tags is None else tags\n"
                "    tags.append(tag)\n"
                "    return tags\n"
            )
        },
    ),
    EvalTask(
        name="rename_across_files",
        prompt=(
            "Rename calc_total to total_price in cart.py and report.py, keeping behavior the "
            "same. test_cart.py already uses the new name."
        ),
        files={
            "cart.py": "def calc_total(prices):\n    return sum(prices)\n",
            "report.py": (
                "from cart import calc_total\n\n\n"
                "def summary(prices):\n    return f'total={calc_total(prices)}'\n"
            ),
            "test_cart.py": (
                "from cart import total_price\nfrom report import summary\n\n\n"
                "def test_total():\n    assert total_price([1, 2, 3]) == 6\n\n\n"
                "def test_summary():\n    assert summary([1, 2]) == 'total=3'\n"
            ),
        },
        solution={
            "cart.py": "def total_price(prices):\n    return sum(prices)\n",
            "report.py": (
                "from cart import total_price\n\n\n"
                "def summary(prices):\n    return f'total={total_price(prices)}'\n"
            ),
        },
    ),
    EvalTask(
        name="fix_string_number_concat",
        prompt="greet.py crashes when given a number. Fix it so the tests pass.",
        files={
            "greet.py": "def greet(name):\n    return 'Hello, ' + name + '!'\n",
            "test_greet.py": (
                "from greet import greet\n\n\n"
                "def test_string():\n"
                "    assert greet('Ada') == 'Hello, Ada!'\n\n\n"
                "def test_number():\n"
                "    assert greet(42) == 'Hello, 42!'\n"
            ),
        },
        solution={"greet.py": "def greet(name):\n    return f'Hello, {name}!'\n"},
    ),
    EvalTask(
        name="fix_swallowed_exception",
        prompt=(
            "parse.py hides real errors by catching everything. Make parse_int raise "
            "ValueError for invalid input instead of returning None, per test_parse.py."
        ),
        files={
            "parse.py": (
                "def parse_int(text):\n"
                "    try:\n"
                "        return int(text)\n"
                "    except Exception:\n"
                "        return None\n"
            ),
            "test_parse.py": (
                "import pytest\n\n"
                "from parse import parse_int\n\n\n"
                "def test_valid():\n"
                "    assert parse_int('42') == 42\n\n\n"
                "def test_invalid_raises():\n"
                "    with pytest.raises(ValueError):\n"
                "        parse_int('nope')\n"
            ),
        },
        solution={"parse.py": "def parse_int(text):\n    return int(text)\n"},
    ),
    EvalTask(
        name="fix_dict_mutation_during_iteration",
        prompt="registry.py raises RuntimeError when pruning. Fix the bug per test_registry.py.",
        files={
            "registry.py": (
                "def prune_empty(registry):\n"
                "    for key, value in registry.items():\n"
                "        if not value:\n"
                "            del registry[key]\n"
                "    return registry\n"
            ),
            "test_registry.py": (
                "from registry import prune_empty\n\n\n"
                "def test_prunes_empty_values():\n"
                "    assert prune_empty({'a': [], 'b': [1]}) == {'b': [1]}\n\n\n"
                "def test_nothing_to_prune():\n"
                "    assert prune_empty({'a': [1]}) == {'a': [1]}\n"
            ),
        },
        solution={
            "registry.py": (
                "def prune_empty(registry):\n"
                "    for key in [key for key, value in registry.items() if not value]:\n"
                "        del registry[key]\n"
                "    return registry\n"
            )
        },
    ),
    EvalTask(
        name="implement_fibonacci",
        prompt="Implement fib(n) in fib.py exactly as test_fib.py expects.",
        files={
            "fib.py": "def fib(n):\n    raise NotImplementedError\n",
            "test_fib.py": (
                "from fib import fib\n\n\n"
                "def test_base():\n"
                "    assert fib(0) == 0\n"
                "    assert fib(1) == 1\n\n\n"
                "def test_recurrence():\n"
                "    assert fib(10) == 55\n"
            ),
        },
        solution={
            "fib.py": (
                "def fib(n):\n"
                "    if n < 2:\n"
                "        return n\n"
                "    a, b = 0, 1\n"
                "    for _ in range(n - 1):\n"
                "        a, b = b, a + b\n"
                "    return b\n"
            )
        },
    ),
    EvalTask(
        name="implement_word_count",
        prompt="Implement word_counts(text) in words.py so test_words.py passes.",
        files={
            "words.py": "def word_counts(text):\n    raise NotImplementedError\n",
            "test_words.py": (
                "from words import word_counts\n\n\n"
                "def test_counts():\n"
                "    assert word_counts('a b a') == {'a': 2, 'b': 1}\n\n\n"
                "def test_empty():\n"
                "    assert word_counts('') == {}\n"
            ),
        },
        solution={
            "words.py": (
                "def word_counts(text):\n"
                "    counts = {}\n"
                "    for word in text.split():\n"
                "        counts[word] = counts.get(word, 0) + 1\n"
                "    return counts\n"
            )
        },
    ),
    EvalTask(
        name="fix_missing_key_access",
        prompt=(
            "settings.py raises KeyError for missing keys. "
            "Make it return the default per test_settings.py."
        ),
        files={
            "settings.py": (
                "def get_setting(settings, key, default=None):\n    return settings[key]\n"
            ),
            "test_settings.py": (
                "from settings import get_setting\n\n\n"
                "def test_present():\n"
                "    assert get_setting({'a': 1}, 'a') == 1\n\n\n"
                "def test_missing_returns_default():\n"
                "    assert get_setting({'a': 1}, 'b', 0) == 0\n"
            ),
        },
        solution={
            "settings.py": (
                "def get_setting(settings, key, default=None):\n"
                "    return settings.get(key, default)\n"
            )
        },
    ),
    EvalTask(
        name="fix_greedy_regex",
        prompt="extract.py captures too much. Fix the regex so test_extract.py passes.",
        files={
            "extract.py": (
                "import re\n\n\ndef extract_tags(text):\n    return re.findall(r'<(.*)>', text)\n"
            ),
            "test_extract.py": (
                "from extract import extract_tags\n\n\n"
                "def test_two_tags():\n"
                "    assert extract_tags('<a><b>') == ['a', 'b']\n\n\n"
                "def test_one_tag():\n"
                "    assert extract_tags('x<a>y') == ['a']\n"
            ),
        },
        solution={
            "extract.py": (
                "import re\n\n\n"
                "def extract_tags(text):\n"
                "    return re.findall(r'<([^>]*)>', text)\n"
            )
        },
    ),
    EvalTask(
        name="fix_sort_key",
        prompt="sorting.py sorts strings case-sensitively. Fix it so test_sorting.py passes.",
        files={
            "sorting.py": ("def sorted_names(names):\n    return sorted(names)\n"),
            "test_sorting.py": (
                "from sorting import sorted_names\n\n\n"
                "def test_case_insensitive():\n"
                "    assert sorted_names(['apple', 'Banana', 'cherry']) == "
                "['apple', 'Banana', 'cherry']\n"
            ),
        },
        solution={
            "sorting.py": ("def sorted_names(names):\n    return sorted(names, key=str.casefold)\n")
        },
    ),
    EvalTask(
        name="implement_to_dict",
        prompt="Implement to_dict on the Person class in person.py per test_person.py.",
        files={
            "person.py": (
                "class Person:\n"
                "    def __init__(self, name, age):\n"
                "        self.name = name\n"
                "        self.age = age\n"
            ),
            "test_person.py": (
                "from person import Person\n\n\n"
                "def test_to_dict():\n"
                "    assert Person('Ada', 36).to_dict() == {'name': 'Ada', 'age': 36}\n"
            ),
        },
        solution={
            "person.py": (
                "class Person:\n"
                "    def __init__(self, name, age):\n"
                "        self.name = name\n"
                "        self.age = age\n\n"
                "    def to_dict(self):\n"
                "        return {'name': self.name, 'age': self.age}\n"
            )
        },
    ),
    EvalTask(
        name="fix_rounding_total",
        prompt=(
            "money.py produces float artifacts. "
            "Fix totals to round to two decimals per test_money.py."
        ),
        files={
            "money.py": ("def total(prices):\n    return sum(prices)\n"),
            "test_money.py": (
                "from money import total\n\n\n"
                "def test_no_float_artifacts():\n"
                "    assert total([0.1, 0.2]) == 0.3\n"
            ),
        },
        solution={"money.py": ("def total(prices):\n    return round(sum(prices), 2)\n")},
    ),
    EvalTask(
        name="fix_class_state_leak",
        prompt="counter.py shares state between instances. Fix it so test_counter.py passes.",
        files={
            "counter.py": (
                "class Counter:\n"
                "    history = []\n\n"
                "    def increment(self):\n"
                "        self.history.append(1)\n"
                "        return len(self.history)\n"
            ),
            "test_counter.py": (
                "from counter import Counter\n\n\n"
                "def test_instances_are_independent():\n"
                "    first = Counter()\n"
                "    second = Counter()\n"
                "    first.increment()\n"
                "    assert second.increment() == 1\n"
            ),
        },
        solution={
            "counter.py": (
                "class Counter:\n"
                "    def __init__(self):\n"
                "        self.history = []\n\n"
                "    def increment(self):\n"
                "        self.history.append(1)\n"
                "        return len(self.history)\n"
            )
        },
    ),
    EvalTask(
        name="move_function_to_module",
        prompt=(
            "Move format_price from shop.py into a new module format.py, keep shop.py "
            "behavior identical by importing from format.py. test_shop.py must pass."
        ),
        files={
            "shop.py": (
                "def format_price(amount):\n"
                "    return f'${amount:.2f}'\n\n\n"
                "def checkout(amount):\n"
                "    return format_price(amount)\n"
            ),
            "test_shop.py": (
                "import format\n\n"
                "from shop import checkout\n\n\n"
                "def test_checkout():\n"
                "    assert checkout(3) == '$3.00'\n\n\n"
                "def test_format_module():\n"
                "    assert format.format_price(3) == '$3.00'\n"
            ),
        },
        solution={
            "format.py": ("def format_price(amount):\n    return f'${amount:.2f}'\n"),
            "shop.py": (
                "from format import format_price\n\n\n"
                "def checkout(amount):\n"
                "    return format_price(amount)\n"
            ),
        },
    ),
    EvalTask(
        name="fix_generator_exhaustion",
        prompt="pages.py can only be iterated once. Fix it so test_pages.py passes.",
        files={
            "pages.py": (
                "def make_pages(items, size):\n"
                "    page = []\n"
                "    for item in items:\n"
                "        page.append(item)\n"
                "        if len(page) == size:\n"
                "            yield page\n"
                "            page = []\n"
                "    if page:\n"
                "        yield page\n\n\n"
                "class Paginator:\n"
                "    def __init__(self, items, size):\n"
                "        self.pages = make_pages(items, size)\n\n"
                "    def __iter__(self):\n"
                "        return self.pages\n"
            ),
            "test_pages.py": (
                "from pages import Paginator\n\n\n"
                "def test_iterate_twice():\n"
                "    paginator = Paginator([1, 2, 3], 2)\n"
                "    assert list(paginator) == [[1, 2], [3]]\n"
                "    assert list(paginator) == [[1, 2], [3]]\n"
            ),
        },
        solution={
            "pages.py": (
                "def make_pages(items, size):\n"
                "    page = []\n"
                "    for item in items:\n"
                "        page.append(item)\n"
                "        if len(page) == size:\n"
                "            yield page\n"
                "            page = []\n"
                "    if page:\n"
                "        yield page\n\n\n"
                "class Paginator:\n"
                "    def __init__(self, items, size):\n"
                "        self._items = list(items)\n"
                "        self._size = size\n\n"
                "    def __iter__(self):\n"
                "        return make_pages(list(self._items), self._size)\n"
            ),
        },
    ),
    EvalTask(
        name="implement_boundary_check",
        prompt="Implement clamp(value, low, high) in clamp.py per test_clamp.py.",
        files={
            "clamp.py": "def clamp(value, low, high):\n    raise NotImplementedError\n",
            "test_clamp.py": (
                "from clamp import clamp\n\n\n"
                "def test_below():\n"
                "    assert clamp(1, 5, 10) == 5\n\n\n"
                "def test_within():\n"
                "    assert clamp(7, 5, 10) == 7\n\n\n"
                "def test_above():\n"
                "    assert clamp(99, 5, 10) == 10\n"
            ),
        },
        solution={
            "clamp.py": ("def clamp(value, low, high):\n    return max(low, min(value, high))\n")
        },
    ),
    EvalTask(
        name="fix_kwargs_collision",
        prompt="apply.py overwrites the original key. Fix it so test_apply.py passes.",
        files={
            "apply.py": (
                "def apply_defaults(config, **defaults):\n"
                "    config.update(defaults)\n"
                "    return config\n"
            ),
            "test_apply.py": (
                "from apply import apply_defaults\n\n\n"
                "def test_defaults_do_not_override():\n"
                "    assert apply_defaults({'mode': 'fast'}, mode='slow') == {'mode': 'fast'}\n\n\n"
                "def test_defaults_fill_missing():\n"
                "    assert apply_defaults({}, mode='slow') == {'mode': 'slow'}\n"
            ),
        },
        solution={
            "apply.py": (
                "def apply_defaults(config, **defaults):\n"
                "    merged = dict(defaults)\n"
                "    merged.update(config)\n"
                "    return merged\n"
            )
        },
    ),
    EvalTask(
        name="fix_bool_return",
        prompt=(
            "validate.py returns the wrong truthiness for empty strings. Fix per test_validate.py."
        ),
        files={
            "validate.py": (
                "def is_valid_username(name):\n"
                "    return not name or len(name) > 20 or not name.isalnum()\n"
            ),
            "test_validate.py": (
                "from validate import is_valid_username\n\n\n"
                "def test_valid():\n"
                "    assert is_valid_username('ada1815') is True\n\n\n"
                "def test_empty():\n"
                "    assert is_valid_username('') is False\n\n\n"
                "def test_too_long():\n"
                "    assert is_valid_username('a' * 21) is False\n\n\n"
                "def test_symbols():\n"
                "    assert is_valid_username('bad!') is False\n"
            ),
        },
        solution={
            "validate.py": (
                "def is_valid_username(name):\n"
                "    return bool(name) and len(name) <= 20 and name.isalnum()\n"
            )
        },
    ),
    EvalTask(
        name="multi_step_config_refactor",
        prompt=(
            "Refactor config.py: extract the DEFAULTS dict into a new defaults.py module, "
            "import it in config.py, and add a get_default(key) function in defaults.py. "
            "All tests in test_config_task.py must pass."
        ),
        files={
            "config.py": (
                "DEFAULTS = {'host': 'localhost', 'port': 8080, 'debug': False}\n\n\n"
                "def current():\n"
                "    return dict(DEFAULTS)\n"
            ),
            "test_config_task.py": (
                "import config\n"
                "import defaults\n\n\n"
                "def test_current():\n"
                "    assert config.current()['port'] == 8080\n\n\n"
                "def test_get_default():\n"
                "    assert defaults.get_default('host') == 'localhost'\n\n\n"
                "def test_get_default_missing():\n"
                "    assert defaults.get_default('nope') is None\n"
            ),
        },
        solution={
            "defaults.py": (
                "DEFAULTS = {'host': 'localhost', 'port': 8080, 'debug': False}\n\n\n"
                "def get_default(key):\n"
                "    return DEFAULTS.get(key)\n"
            ),
            "config.py": (
                "from defaults import DEFAULTS\n\n\ndef current():\n    return dict(DEFAULTS)\n"
            ),
        },
    ),
    EvalTask(
        name="read_line_slice",
        prompt=(
            "Use read_file with start_line and end_line to read lines 20-25 of notes.txt "
            "(1-based, inclusive) and create slice.txt containing exactly those lines."
        ),
        files={
            "notes.txt": "".join(f"line {number:02d}\n" for number in range(1, 41)),
            "test_slice.py": (
                "from pathlib import Path\n\n\n"
                "def test_slice_matches():\n"
                "    expected = ''.join(f'line {number:02d}\\n' for number in range(20, 26))\n"
                "    assert Path('slice.txt').read_text(encoding='utf-8') == expected\n"
            ),
        },
        solution={"slice.txt": "".join(f"line {number:02d}\n" for number in range(20, 26))},
    ),
    EvalTask(
        name="edit_file_replace_all",
        prompt=(
            "config.py contains the old host name twice. Use edit_file to replace every "
            "occurrence of 'old.example' with 'new.example'. Expect the first attempt to "
            "fail because the target string is not unique, then handle it correctly."
        ),
        files={
            "config.py": ("HOST = 'old.example'\nFALLBACK = 'old.example'\nPORT = 8080\n"),
            "test_config_task.py": (
                "from config import FALLBACK, HOST\n\n\n"
                "def test_hosts_replaced():\n"
                "    assert HOST == 'new.example' and FALLBACK == 'new.example'\n"
            ),
        },
        solution={"config.py": "HOST = 'new.example'\nFALLBACK = 'new.example'\nPORT = 8080\n"},
    ),
    EvalTask(
        name="create_file_overwrite_refusal",
        prompt=(
            "Create a new file summary.md with the single line 'summary: ready'. report.md "
            "already exists: do not destroy its content; add a final line 'updated: yes' "
            "using edit_file instead of overwriting the whole file."
        ),
        files={
            "report.md": "# Report\n\noriginal body\n",
            "test_report.py": (
                "from pathlib import Path\n\n\n"
                "def test_summary_created():\n"
                "    assert Path('summary.md').read_text(encoding='utf-8').strip() "
                "== 'summary: ready'\n\n\n"
                "def test_report_preserved_and_updated():\n"
                "    text = Path('report.md').read_text(encoding='utf-8')\n"
                "    assert 'original body' in text and 'updated: yes' in text\n"
            ),
        },
        solution={
            "summary.md": "summary: ready\n",
            "report.md": "# Report\n\noriginal body\nupdated: yes\n",
        },
    ),
    EvalTask(
        name="undo_restores_checkpoint",
        prompt=(
            "Use edit_file to change the VERSION line in a.txt from 'version = 1' to "
            "'version = 2'. Nothing else should change."
        ),
        files={
            "a.txt": "title = demo\nversion = 1\n",
            "undo_check.py": (
                "import json\n"
                "import sys\n"
                "from pathlib import Path\n\n"
                "from eurus.coding.checkpoint import CheckpointStore\n\n\n"
                "def main() -> int:\n"
                "    root = Path.cwd()\n"
                "    store = CheckpointStore(home=root)\n"
                "    manifests = sorted((root / 'undo').glob('*.json'))\n"
                "    if not manifests:\n"
                "        print('no checkpoint manifest found')\n"
                "        return 1\n"
                "    run_id = manifests[0].stem\n"
                "    restored, refused = store.undo(run_id, root)\n"
                "    if 'a.txt' not in restored:\n"
                "        print(f'a.txt not restored: restored={restored} refused={refused}')\n"
                "        return 1\n"
                "    if 'version = 1' not in Path('a.txt').read_text(encoding='utf-8'):\n"
                "        print('a.txt was not restored to its original content')\n"
                "        return 1\n"
                "    return 0\n\n\n"
                "if __name__ == '__main__':\n"
                "    sys.exit(main())\n"
            ),
            "test_a_task.py": (
                "from pathlib import Path\n\n\n"
                "def test_version_updated():\n"
                "    assert 'version = 2' in Path('a.txt').read_text(encoding='utf-8')\n"
            ),
        },
        solution={"a.txt": "title = demo\nversion = 2\n"},
        check=(sys.executable, "undo_check.py"),
    ),
)
