import pytest

from segue import analysis, changes, edits, select
from segue.model import Playlist, PlaylistEntry, ServiceRef

from .conftest import make_track


def add(store, name, *titles, kind="created", source=None, artists=None, dates=None):
    entries = []
    for i, t in enumerate(titles):
        track = make_track(t, [artists[i]] if artists else ["Artist"],
                           release_date=dates[i] if dates else None)
        entries.append(PlaylistEntry(track))
    pid, _ = store.save_playlist(Playlist(name, entries, kind=kind, source=source))
    return pid


def titles(store, pid):
    return [t.title for t in store.get_playlist(pid).tracks]


def do(store, plan):
    return changes.submit(store, *plan, mode="safe")[0]


def ids(store):
    return [s.id for s in store.playlist_summaries()]


# Filters

def test_filters(store):
    gym = add(store, "Gym", "A", "B")
    empty = add(store, "Empty")
    small = add(store, "Gym small", "A")
    theirs = add(store, "Gym theirs", "C", kind="followed")
    spotify = add(store, "Spot", "D", source=ServiceRef("spotify", "p1"))
    every = ids(store)
    assert select.apply_filter(store, every, select.Filter(empty=True)) == [empty]
    assert select.apply_filter(store, every, select.Filter(name="gym*")) == [gym, small]
    assert select.apply_filter(store, every, select.Filter(name="gym*", kinds={"followed"})) == [theirs]
    assert select.apply_filter(store, every, select.Filter(name=r"^gym$", regex=True)) == [gym]
    assert select.apply_filter(store, every, select.Filter(max_tracks=1, min_tracks=1)) == [small, spotify]
    assert select.apply_filter(store, every, select.Filter(service="spotify")) == [spotify]
    assert select.apply_filter(store, every, select.Filter(subset=True)) == [small]


def test_identical_filter_selects_only_newer_copies(store):
    first = add(store, "Mix", "A", "B")
    copy = add(store, "Mix again", "B", "A")
    add(store, "Different", "A", "C")
    assert select.apply_filter(store, ids(store), select.Filter(identical=True)) == [copy]
    assert first not in select.apply_filter(store, ids(store), select.Filter(identical=True))


@pytest.mark.parametrize("a, b", [
    ("Gym", "gym "), ("Gym", "GYM (1)"), ("Gym", "Gym 💪"), ("Gym", "Gym - copy"), ("Gym", "Gym (Copy 2)"),
])
def test_same_names(a, b):
    assert select.name_key(a) == select.name_key(b)


@pytest.mark.parametrize("a, b", [("Gym", "Gym 2"), ("Carbon Copy", "Carbon"), ("Café", "Cafe")])
def test_different_names(a, b):
    assert select.name_key(a) != select.name_key(b)


# Merge by name

def test_merge_by_name_per_service(store):
    old = add(store, "Gym", "A", "B")
    dup = add(store, "gym (1)", "B", "C")
    other_service = add(store, "Gym", "X", source=ServiceRef("spotify", "s1"))
    add(store, "Gym 2", "Z")
    description, ops, groups = edits.plan_merge_by_name(store, ids(store))
    assert groups == [("Gym", ["gym (1)"])]
    do(store, (description, ops))
    assert titles(store, old) == ["A", "B", "C"]
    assert store.playlist_state(dup) is None
    assert titles(store, other_service) == ["X"]


def test_merge_by_name_largest_survivor_and_keep_sources(store):
    add(store, "Run", "A")
    big = add(store, "run", "B", "C", "D")
    _, ops, groups = edits.plan_merge_by_name(store, ids(store), delete_sources=False)
    assert groups == [("run", ["Run"])]
    do(store, ("merge", ops))
    assert titles(store, big) == ["B", "C", "D", "A"]
    assert len(store.playlist_summaries()) == 2


# Bulk rename

def test_bulk_rename_rules(store):
    a = add(store, "  my   gym (1)")
    b = add(store, "my run")
    rules = edits.RenameRules(replace=[("my ", "", False)], strip_copies=True, trim=True, case="title",
                              prefix="★ ")
    do(store, edits.plan_bulk_rename(store, [a, b], rules))
    assert [store.get_playlist(p).name for p in (a, b)] == ["★ Gym", "★ Run"]


def test_bulk_rename_regex_and_no_change(store):
    a = add(store, "2023 Summer")
    b = add(store, "Winter")
    _, ops = edits.plan_bulk_rename(store, [a, b], edits.RenameRules(replace=[(r"^(\d{4}) (.*)", r"\2 \1", True)]))
    assert len(ops) == 1
    do(store, ("rename", ops))
    assert store.get_playlist(a).name == "Summer 2023"


# Sort and shuffle

def test_sort_multi_key_with_missing_values_last(store):
    pid = add(store, "Mix", "b", "a", "c", "d", artists=["Y", "X", "X", "Y"],
              dates=["2001", None, "1999", "2010"])
    do(store, edits.plan_sort(store, pid, ["artist", "title"]))
    assert titles(store, pid) == ["a", "c", "b", "d"]
    do(store, edits.plan_sort(store, pid, ["release"], reverse=True))
    assert titles(store, pid) == ["d", "b", "c", "a"]  # undated "a" last even when reversed


def test_sort_as_new_leaves_the_original(store):
    pid = add(store, "Mix", "b", "a")
    do(store, edits.plan_sort(store, pid, ["title"], as_new="Mix sorted"))
    assert titles(store, pid) == ["b", "a"]
    [new] = store.find_playlists("Mix sorted")
    assert titles(store, new.id) == ["a", "b"]


def test_sorting_liked_songs_needs_a_new_playlist(store):
    liked = add(store, "Liked Songs", "b", "a", kind="liked")
    with pytest.raises(ValueError, match="--as-new"):
        edits.plan_sort(store, liked, ["title"])
    with pytest.raises(ValueError, match="can't sort by"):
        edits.plan_sort(store, liked, ["bpm"], as_new="x")


def test_shuffle_is_repeatable_with_a_seed(store):
    pid = add(store, "Mix", *"abcdefgh")
    _, ops1 = edits.plan_shuffle(store, pid, seed=4)
    _, ops2 = edits.plan_shuffle(store, pid, seed=4)
    assert ops1 == ops2 and ops1[0].order != tuple(range(8))


# Split and clone

def test_split_every_n(store):
    pid = add(store, "Big", *"abcde")
    do(store, edits.plan_split(store, pid, every=2))
    names = [s.name for s in store.playlist_summaries()]
    assert "Big – part 3" in names and "Big" in names
    [part3] = store.find_playlists("Big – part 3")
    assert titles(store, part3.id) == ["e"]


def test_split_by_decade_with_minimum(store):
    pid = add(store, "Old", "a", "b", "c", "d", dates=["1991", "1995", "2003", None])
    do(store, edits.plan_split(store, pid, by="decade", min_size=2, delete_original=True))
    names = sorted(s.name for s in store.playlist_summaries())
    assert names == ["Old – 1990s", "Old – other"]
    [other] = store.find_playlists("Old – other")
    assert titles(store, other.id) == ["c", "d"]


def test_split_into_one_piece_is_refused(store):
    pid = add(store, "Solo", "a", "b", artists=["X", "X"])
    with pytest.raises(ValueError, match="one piece"):
        edits.plan_split(store, pid, by="artist")


def test_clone(store):
    pid = add(store, "Mix", "a", "b")
    do(store, edits.plan_clone(store, pid))
    [copy] = store.find_playlists("Mix (copy)")
    assert titles(store, copy.id) == ["a", "b"]


# Find and check

def test_where(store):
    a = add(store, "A", "Hey Jude", "Other")
    b = add(store, "B", "x", "Hey Jude")
    [hit] = analysis.where(store, "hey jude")
    assert [(pid, pos) for pid, _, pos in hit.places] == [(a, [1]), (b, [2])]
    assert analysis.where(store, "nothing like this") == []


def test_loose_tracks(store):
    gone = add(store, "Gone", "lonely")
    add(store, "Liked Songs", "liked only", "shared", kind="liked")
    add(store, "Mine", "shared")
    do(store, edits.plan_delete(store, [gone]))
    assert [t.title for t in analysis.unplaylisted(store)] == ["lonely"]
    assert [t.title for t in analysis.liked_unplaylisted(store)] == ["liked only"]


def test_overlaps(store):
    a = add(store, "A", "1", "2", "3", "4", "5")
    b = add(store, "B", "1", "2", "3", "4", "5", "6")
    c = add(store, "C", "1", "2")
    add(store, "Other service", "1", "2", source=ServiceRef("spotify", "x"))
    found = {(o.a, o.b): o.kind for o in analysis.overlaps(store)}
    assert found[(a, b)] == "subset" and found[(c, a)] == "subset"
    assert all(o.kind != "identical" for o in analysis.overlaps(store))


def test_check_reports_and_suggests_fixes(store):
    add(store, "Empty")
    add(store, "Gym", "a", "a")
    add(store, "gym (1)", "b")
    add(store, "Copy one", "x", "y")
    add(store, "Copy two", "y", "x")
    findings = {f.title: f.fix for f in analysis.check(store)}
    assert findings["1 empty playlist"] == "segue delete --empty"
    assert findings["1 name used by more than one playlist"] == "segue merge-names --dry-run"
    assert findings["1 pair of differently named playlists with the same tracks"] == "segue delete --identical --dry-run"
    assert any(t.startswith("1 playlist with duplicates") for t in findings)


def test_check_doesnt_repeat_what_merge_names_covers(store):
    add(store, "Gym", "a", "b")
    add(store, "Gym 💪", "a")
    titles_ = [f.title for f in analysis.check(store)]
    assert titles_ == ["1 name used by more than one playlist"]


def test_check_groups_overlaps_of_copies(store):
    big = add(store, "Big", "a", "b", "c", "d")
    add(store, "Big", "a", "b", "c", "d")
    add(store, "Small", "a")
    add(store, "Small", "a")
    findings = {f.title: f for f in analysis.check(store)}
    inside = findings["1 playlist entirely inside another"]
    assert inside.lines == ["'Small' (1 track) is entirely inside 'Big'"]


def test_tidy_library_has_nothing_to_report(store):
    add(store, "One", "a")
    add(store, "Two", "b")
    assert analysis.check(store) == []
