from __future__ import annotations

from skills.defect_hunts.h2_derive import candidate_id, derive


def test_candidate_id_format():
    assert candidate_id("redis", "pkg/a.py", "redis.from_url", 1) == "redis:pkg/a.py:redis.from_url#1"


def test_id_is_churn_invariant_under_line_insertion():
    base = "import redis\nX = redis.from_url('u')\n"
    perturbed = "import redis\n# a new comment line\n\nX = redis.from_url('u')\n"
    diff_b = "diff --git a/pkg/a.py b/pkg/a.py\n+X = redis.from_url('u')\n"
    diff_p = "diff --git a/pkg/a.py b/pkg/a.py\n+X = redis.from_url('u')\n"

    [a] = derive({"pkg/a.py": base}, diff_b)
    [b] = derive({"pkg/a.py": perturbed}, diff_p)

    assert a.id == b.id


def test_repeated_identical_calls_get_distinct_ids():
    src = "import redis\nA = redis.from_url('u')\nB = redis.from_url('u')\n"
    diff = "diff --git a/pkg/a.py b/pkg/a.py\n+A = redis.from_url('u')\n+B = redis.from_url('u')\n"

    ids = sorted(c.id for c in derive({"pkg/a.py": src}, diff))

    assert ids == ["redis:pkg/a.py:redis.from_url#1", "redis:pkg/a.py:redis.from_url#2"]


def test_derives_redis_call_in_added_lines():
    src = "import redis\nX = redis.from_url('u')\n"
    diff = "diff --git a/pkg/a.py b/pkg/a.py\n+X = redis.from_url('u')\n"

    [c] = derive({"pkg/a.py": src}, diff)

    assert c.kind == "redis"


def test_bare_import_without_call_does_not_derive():
    src = "import redis\n"
    diff = "diff --git a/pkg/a.py b/pkg/a.py\n+import redis\n"

    assert derive({"pkg/a.py": src}, diff) == []


def test_comment_or_string_mention_does_not_derive():
    src = "X = 1  # redis.from_url is great\nY = 'redis.from_url'\n"
    diff = "diff --git a/pkg/a.py b/pkg/a.py\n+X = 1  # redis.from_url is great\n"

    assert derive({"pkg/a.py": src}, diff) == []


def test_call_only_in_unchanged_lines_does_not_derive():
    src = "import redis\nX = redis.from_url('u')\n"
    diff = "diff --git a/pkg/a.py b/pkg/a.py\n+UNRELATED = 1\n"

    assert derive({"pkg/a.py": src}, diff) == []


def test_noop_guarded_try_call_is_excluded():
    src = "import requests\ntry:\n    requests.get('u')\nexcept Exception:\n    pass\n"
    diff = "diff --git a/pkg/a.py b/pkg/a.py\n+    requests.get('u')\n"

    assert derive({"pkg/a.py": src}, diff) == []


def test_os_environ_get_is_not_an_h2_candidate():
    src = "import os\nX = os.environ.get('K', '')\n"
    diff = "diff --git a/pkg/a.py b/pkg/a.py\n+X = os.environ.get('K', '')\n"

    assert derive({"pkg/a.py": src}, diff) == []


def test_realistic_single_file_diff_derives_at_most_K3_candidates():
    src = (
        "import redis, psycopg, subprocess\n"
        "A=redis.from_url('u')\n"
        "B=psycopg.connect('d')\n"
        "C=subprocess.run(['x'])\n"
    )
    diff = (
        "diff --git a/pkg/a.py b/pkg/a.py\n"
        "+A=redis.from_url('u')\n"
        "+B=psycopg.connect('d')\n"
        "+C=subprocess.run(['x'])\n"
    )

    assert len(derive({"pkg/a.py": src}, diff)) <= 3
