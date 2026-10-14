"""Legacy selection oracles and operation growth, using only synthetic history."""

import sys
from collections import Counter
from copy import copy
from random import Random

import pytest
from agent_framework import GROUP_ANNOTATION_KEY, Content, Message, annotate_message_groups
from bbx_runtime.ctf import context as sizing
from bbx_runtime.ctf import middleware
from bbx_runtime.ctf.context import estimate_message_lengths, estimate_messages_tokens
from bbx_runtime.ctf.middleware import TurnBoundary
from test_ctf_context import history, provider, result, text


def old_boundaries(messages):
    """The original span expansion and sort, independent of the new sweep."""
    shadows = []
    spans = {}

    def extend(key, index):
        spans[key] = (spans.get(key, (index, index))[0], index)

    for index, message in enumerate(messages):
        shadow = copy(message)
        shadow.additional_properties = dict(message.additional_properties)
        annotation = shadow.additional_properties.get(GROUP_ANNOTATION_KEY)
        if isinstance(annotation, dict):
            shadow.additional_properties[GROUP_ANNOTATION_KEY] = dict(annotation)
            if isinstance(annotation.get("id"), str):
                extend(f"existing:{annotation['id']}", index)
        for content in message.contents:
            if content.type == "text_reasoning" and content.id:
                extend(f"reasoning:{content.id}", index)
        shadows.append(shadow)
    annotate_message_groups(shadows)
    groups, kinds = {}, {}
    for index, message in enumerate(shadows):
        annotation = message.additional_properties[GROUP_ANNOTATION_KEY]
        group_id = annotation["id"]
        groups.setdefault(group_id, []).append(index)
        kinds[group_id] = annotation["kind"]
        extend(f"native:{group_id}", index)
    forbidden = {cut for first, last in spans.values() for cut in range(first + 1, last + 1)}
    unfinished = len(messages)
    candidates = []
    for group_id, indices in groups.items():
        declared = {
            c.call_id
            for i in indices
            for c in messages[i].contents
            if c.type == "function_call" and c.call_id and not c.informational_only
        }
        returned = {
            c.call_id
            for i in indices
            for c in messages[i].contents
            if c.type == "function_result" and c.call_id
        }
        if declared - returned:
            unfinished = min(unfinished, indices[0])
        if kinds[group_id] in {"assistant_text", "tool_call"}:
            candidates.append(indices[-1] + 1)
    return (
        sorted(c for c in candidates if c <= unfinished and c not in forbidden),
        forbidden,
        unfinished,
    )


def old_align(boundaries, start):
    completed, forbidden, unfinished = boundaries
    if start == 0:
        return 0
    if start > unfinished:
        return None
    if start not in forbidden:
        return start
    return next((cut for cut in completed if cut > start), None)


def random_history(rng, size):
    messages = []
    for index in range(size):
        kind = rng.randrange(6)
        if kind == 0:
            call_id = f"call-{rng.randrange(max(1, size // 3))}"
            messages.append(
                Message(
                    role="assistant",
                    message_id=f"m-{index}",
                    contents=[
                        Content.from_function_call(
                            call_id, "inspect", arguments={"q": "x" * rng.randrange(60)}
                        )
                    ],
                )
            )
        elif kind == 1:
            messages.append(
                result(f"call-{rng.randrange(max(1, size // 3))}", "!" * rng.randrange(100))
            )
        elif kind == 2:
            content = Content.from_text_reasoning(
                text="summary" * rng.randrange(10), id=f"r-{rng.randrange(4)}"
            )
            if rng.randrange(2):
                content.protected_data = "opaque"
            messages.append(Message(role="assistant", message_id=f"m-{index}", contents=[content]))
        else:
            messages.append(
                text(f"m-{index}", "x!汉" * rng.randrange(60), "assistant" if kind == 3 else "user")
            )
    # Native annotations are sometimes complete and sometimes followed by a new suffix.
    annotated = rng.randrange(len(messages) + 1)
    annotate_message_groups(messages[:annotated])
    return messages


def test_randomized_native_boundaries_match_legacy_without_mutating_history():
    rng = Random(9231)
    for _ in range(100):
        messages = random_history(rng, rng.randrange(1, 45))
        before = [message.to_dict() for message in messages]
        old = old_boundaries(messages)
        new = sizing.history_boundaries(messages)
        assert (new.completed, new.forbidden, new.unfinished_from) == old
        for start in range(len(messages) + 1):
            assert new.align(start) == old_align(old, start)
        assert [message.to_dict() for message in messages] == before


def old_selection(value, messages, prefix, start, confirmed_count, *, leaf=False):
    """Keep the former per-candidate prefix scan, ID rebuild and suffix sum."""
    protected = value._protected_input_ids()
    previous = value._recovered_inputs()
    lengths = estimate_message_lengths(messages, provider=value.model.provider)
    eligible = confirmed_count
    for index in range(start, confirmed_count):
        if messages[index].message_id in protected and not value._can_relocate_input(
            messages[index]
        ):
            eligible = min(eligible, index)
    cut = start
    remaining = sum(lengths[start:])
    base = value.fixed_input_tokens
    limit = max(
        0,
        value.input_limit - base - estimate_messages_tokens(prefix, provider=value.model.provider),
    )
    overhead = base + estimate_messages_tokens(prefix, provider=value.model.provider)
    pinned = previous
    if not leaf and remaining <= limit:
        return cut, prefix, remaining, False
    for boundary in old_boundaries(messages)[0]:
        if boundary <= start or boundary > eligible:
            continue
        moved = [message for message in messages[start:boundary] if message.message_id in protected]
        candidate = [
            *previous,
            *[
                message
                for message in moved
                if message.message_id not in {m.message_id for m in previous}
            ],
        ]
        if leaf:
            remaining = (
                overhead
                + sum(lengths[boundary:])
                + estimate_messages_tokens(
                    candidate[len(previous) :], provider=value.model.provider
                )
            )
            cut, pinned = boundary, candidate
            if remaining <= base + int((value.input_limit - base) * 0.6):
                break
        else:
            candidate_prefix = [prefix[0], *candidate] if prefix else []
            candidate_limit = max(
                0,
                value.input_limit
                - base
                - estimate_messages_tokens(candidate_prefix, provider=value.model.provider),
            )
            candidate_remaining = sum(lengths[boundary:])
            if candidate_remaining <= candidate_limit:
                cut, remaining, limit, pinned = (
                    boundary,
                    candidate_remaining,
                    candidate_limit,
                    candidate,
                )
                if remaining <= int(limit * 0.6):
                    break
    if leaf:
        return (
            cut,
            [*prefix, *pinned[len(previous) :]],
            remaining,
            not cut or remaining > value.input_limit,
        )
    return cut, [prefix[0], *pinned] if prefix else [], remaining, remaining > limit


def seed_state(previous):
    seed = text("seed", "Synthetic recovery")
    return {
        "ctf_context_recovery_seed": seed.to_dict(),
        "ctf_context_recovery": {
            "turn_id": "turn",
            "message": seed.to_dict(),
            "protected_inputs": [m.to_dict() for m in previous],
        },
    }


@pytest.mark.parametrize("model_provider", ["openai_chat", "openai_responses"])
async def test_randomized_load_selection_matches_legacy(model_provider):
    rng = Random(1848)
    for _ in range(80):
        messages = random_history(rng, rng.randrange(2, 30))
        previous = [text("recovered", "retain")] if rng.randrange(2) else []
        # Duplicate IDs deliberately retain the original multiplicity behavior.
        protected = [m.message_id for m in messages if m.role == "user" and rng.randrange(2)]
        value = provider(
            messages,
            state=seed_state(previous),
            threshold=rng.randrange(300, 1800),
            fixed=rng.randrange(30),
        )
        value.model = value.model.model_copy(update={"provider": model_provider})
        value.turn["assignment_message_ids"] = protected
        prefix = [
            Message.from_dict(value.checkpoint.session.state["ctf_context_recovery_seed"]),
            *previous,
        ]
        before = [m.to_dict() for m in messages]
        cut, expected_prefix, _, blocked = old_selection(value, messages, prefix, 0, len(messages))
        if blocked:
            with pytest.raises(TurnBoundary):
                await history(value)
        else:
            actual = await history(value)
            assert [m.to_dict() for m in actual] == [
                m.to_dict() for m in [*expected_prefix, *messages[cut:]]
            ]
            assert value.checkpoint.session.state["ctf_history_view"]["excluded_before"] == cut
        assert [m.to_dict() for m in messages] == before


@pytest.mark.parametrize("model_provider", ["openai_chat", "openai_responses"])
async def test_randomized_leaf_selection_matches_legacy(model_provider):
    rng = Random(2491)
    for _ in range(65):
        # Alternating text, complete tool groups and an occasional unknown tail.
        messages = []
        for index in range(rng.randrange(2, 18)):
            messages.extend(
                [
                    text(f"u-{index}", "directive" * rng.randrange(6)),
                    text(f"a-{index}", "x!" * rng.randrange(70), "assistant"),
                ]
            )
        if rng.randrange(2):
            messages.append(
                Message(
                    role="assistant", contents=[Content.from_function_call("unknown", "inspect")]
                )
            )
        previous = [text("recovered", "retain")]
        value = provider(messages, state=seed_state(previous), threshold=100000)
        value.model = value.model.model_copy(update={"provider": model_provider})
        value.turn["assignment_message_ids"] = [
            m.message_id for m in messages if m.role == "user" and rng.randrange(2)
        ]
        view = await history(value)
        value.input_limit = rng.randrange(250, 1400)
        outgoing = [*view, text("fresh", "!" * rng.randrange(200))]
        prefix_count = value._loaded_prefix_count
        prefix, body = outgoing[:prefix_count], outgoing[prefix_count:]
        cut, expected_prefix, remaining, blocked = old_selection(
            value, body, prefix, 0, len(messages), leaf=True
        )
        before = [m.to_dict() for m in messages]
        actual = value._compact_confirmed_prefix(outgoing, outgoing)
        if blocked:
            assert actual is None
        else:
            assert actual is not None
            assert [m.to_dict() for m in actual] == [
                m.to_dict() for m in [*expected_prefix, *body[cut:]]
            ]
            assert (
                value.checkpoint.session.state["ctf_context_compaction"]["remaining"] == remaining
            )
            assert value.checkpoint.session.state["ctf_history_view"]["excluded_before"] == cut
        assert [m.to_dict() for m in messages] == before


@pytest.mark.parametrize("leaf", [False, True])
@pytest.mark.parametrize("model_provider", ["openai_chat", "openai_responses"])
async def test_completed_native_groups_with_existing_cutoff_match_legacy(leaf, model_provider):
    rng = Random(512)
    for _ in range(30):
        messages = [text("evicted-input", "old"), text("evicted-answer", "done", "assistant")]
        for index in range(8):
            messages.append(text(f"u-{index}", "retain" * rng.randrange(12)))
            messages.extend(
                [
                    Message(
                        role="assistant",
                        message_id=f"r-{index}",
                        contents=[
                            Content.from_text_reasoning(
                                text="summary" * rng.randrange(20),
                                id=f"rs-{index}",
                                protected_data="opaque",
                            )
                        ],
                    ),
                    Message(
                        role="assistant",
                        message_id=f"c-{index}",
                        contents=[
                            Content.from_function_call(
                                f"call-{index}", "inspect", arguments={"q": index}
                            )
                        ],
                    ),
                    text(f"notice-{index}", "intervening notice"),
                    result(f"call-{index}", "done"),
                ]
            )
        messages.append(text("current", "fresh input"))
        previous = [text("u-0", "already recovered"), text("recovered", "retain")]
        state = seed_state(previous)
        state["ctf_history_view"] = {"excluded_before": 2, "epoch": 3}
        value = provider(
            messages, state=state, threshold=100000 if leaf else rng.randrange(1500, 5000)
        )
        value.model = value.model.model_copy(update={"provider": model_provider})
        value.turn["assignment_message_ids"] = [f"u-{index}" for index in range(8)] + ["current"]
        prefix = [Message.from_dict(state["ctf_context_recovery_seed"]), *previous]
        before = [m.to_dict() for m in messages]
        if leaf:
            view = await history(value)
            value.input_limit = rng.randrange(1500, 5000)
            body = view[len(prefix) :]
            cut, expected_prefix, _, blocked = old_selection(
                value, body, prefix, 0, len(messages) - 2, leaf=True
            )
            actual = value._compact_confirmed_prefix(view, view)
            assert (actual is None) == blocked
            if not blocked:
                assert actual is not None
                assert [m.to_dict() for m in actual] == [
                    m.to_dict() for m in [*expected_prefix, *body[cut:]]
                ]
                assert (
                    value.checkpoint.session.state["ctf_history_view"]["excluded_before"] == cut + 2
                )
        else:
            cut, expected_prefix, _, blocked = old_selection(
                value, messages, prefix, 2, len(messages)
            )
            if blocked:
                with pytest.raises(TurnBoundary):
                    await history(value)
            else:
                actual = await history(value)
                assert [m.to_dict() for m in actual] == [
                    m.to_dict() for m in [*expected_prefix, *messages[cut:]]
                ]
                assert value.checkpoint.session.state["ctf_history_view"]["excluded_before"] == cut
        assert [m.to_dict() for m in messages] == before


class WorkCounter:
    def __init__(self):
        self.counts = Counter()

    def trace(self, frame, event, arg):
        if event == "line" and frame.f_code.co_filename in {sizing.__file__, middleware.__file__}:
            self.counts["python_lines"] += 1
        return self.trace

    def instrument(self, monkeypatch):
        original = sizing.annotate_message_groups

        def annotate(messages):
            self.counts["native_calls"] += 1
            self.counts["native_messages"] += len(messages)
            return original(messages)

        monkeypatch.setattr(sizing, "annotate_message_groups", annotate)
        original_lengths = middleware.estimate_message_lengths
        counter = self

        class Lengths(list):
            def __getitem__(self, key):
                result = super().__getitem__(key)
                if isinstance(key, slice):
                    counter.counts["length_work"] += len(result)
                return result

            def __iter__(self):
                counter.counts["length_work"] += len(self)
                return super().__iter__()

        def lengths(messages, **kwargs):
            self.counts["estimated_messages"] += len(messages)
            return Lengths(original_lengths(messages, **kwargs))

        monkeypatch.setattr(middleware, "estimate_message_lengths", lengths)


@pytest.mark.parametrize("leaf", [False, True])
async def test_compaction_operation_growth_is_linear(monkeypatch, leaf):
    counts = []
    for size in (64, 128, 256):
        messages = [
            m
            for i in range(size)
            for m in (text(f"u-{i}", "u"), text(f"a-{i}", "x" * 60, "assistant"))
        ]
        previous = [text(f"prior-{i}", "p") for i in range(size // 4)]
        value = provider(messages, state=seed_state(previous), threshold=100000)
        value.turn["assignment_message_ids"] = [f"u-{i}" for i in range(size)]
        view = await history(value) if leaf else None
        value.input_limit = (
            estimate_messages_tokens(
                [text("seed", "Synthetic recovery"), *previous, *messages[::2]]
            )
            + 30
        )
        counter = WorkCounter()
        with monkeypatch.context() as local:
            counter.instrument(local)
            sys.settrace(counter.trace)
            try:
                if leaf:
                    assert view is not None
                    assert value._compact_confirmed_prefix(view, view) is not None
                else:
                    await history(value)
            finally:
                sys.settrace(None)
        assert counter.counts["native_calls"] == (2 if leaf else 1)
        assert value.checkpoint.session.state["ctf_history_view"]["excluded_before"] > size
        counts.append(counter.counts)
    for previous_count, current in zip(counts, counts[1:], strict=False):
        for key in ("python_lines", "native_messages", "length_work", "estimated_messages"):
            assert current[key] <= previous_count[key] * 2.2, (key, counts)


def test_overlapping_reasoning_span_work_is_linear():
    counts = []
    for size in (64, 128, 256):
        messages = [
            Message(
                role="user",
                message_id=f"m-{i}",
                contents=[Content.from_text_reasoning(id=f"r-{i % size}")],
            )
            for i in range(size * 2)
        ]
        counter = WorkCounter()
        sys.settrace(counter.trace)
        try:
            boundaries = sizing.history_boundaries(messages)
        finally:
            sys.settrace(None)
        assert boundaries.forbidden == set(range(1, size * 2))
        counts.append(counter.counts["python_lines"])
    assert counts[1] <= counts[0] * 2.1
    assert counts[2] <= counts[1] * 2.1
