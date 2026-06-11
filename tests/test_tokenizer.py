"""
tests/test_tokenizer.py

Token Counting Math Tests.

Purpose:
    Verifies that llm/tokenizer.py produces correct token counts for each tokenizer
    family (tiktoken, char_heuristic) and that the fits_in_budget() function correctly
    gates on the available input token budget.

    The hf_auto tokenizer family is only tested if the [hf-tokenizer] optional extra
    is installed — tests for it are skipped via pytest.importorskip.

    Tests:
    test_tiktoken_nonempty_text         — Asserts count_tokens returns int > 0 for non-empty text.
    test_tiktoken_empty_text            — Asserts count_tokens returns 0 for empty string.
    test_char_heuristic_formula         — Asserts char_heuristic returns len(text) // 4.
    test_fits_in_budget_true            — Short text fits within budget.
    test_fits_in_budget_false           — Very long text exceeds budget.
    test_fits_in_budget_reserved_tokens — Reserved output tokens correctly reduce available budget.
    test_hf_auto_skipped_if_not_installed — hf_auto tests skip gracefully if transformers missing.
    test_unknown_family_raises_value_error — Bad family fails loudly, no silent fallback.
"""

import pytest

from llm.tokenizer import count_tokens, fits_in_budget


def test_tiktoken_nonempty_text():
    """
    Assert count_tokens returns a positive integer for a non-empty English string.

    Purpose:
        Sanity check that the tiktoken encoder is loadable and returns a non-zero
        count for typical prose text. Exact count is not asserted (varies by tokenizer
        version); only > 0 is checked.
    """
    count = count_tokens("The harbor lights flickered against the oncoming storm.", "tiktoken")
    assert isinstance(count, int)
    assert count > 0


def test_tiktoken_empty_text():
    """
    Assert count_tokens returns 0 for an empty string.
    """
    assert count_tokens("", "tiktoken") == 0


def test_char_heuristic_formula():
    """
    Assert char_heuristic tokenizer returns exactly len(text) // 4.

    Purpose:
        The char_heuristic formula (character count ÷ 4) must be exact, not
        rounded or approximated. This is a deterministic formula with no external
        dependencies — any deviation indicates a regression.
    """
    for text in ("", "abc", "abcd", "a" * 17, "word " * 100):
        assert count_tokens(text, "char_heuristic") == len(text) // 4


def test_fits_in_budget_true():
    """
    Assert fits_in_budget returns True for a short text within a generous budget.
    """
    assert fits_in_budget(
        "A short sentence.",
        context_window=8192,
        reserved_output_tokens=2048,
        tokenizer_family="char_heuristic",
    )


def test_fits_in_budget_false():
    """
    Assert fits_in_budget returns False for a text that exceeds the available budget.
    """
    assert not fits_in_budget(
        "x" * 10_000,  # 2500 char_heuristic tokens
        context_window=2048,
        reserved_output_tokens=1024,
        tokenizer_family="char_heuristic",
    )


def test_fits_in_budget_reserved_tokens_reduce_budget():
    """
    Assert that reserved_output_tokens is correctly subtracted from context_window.

    Purpose:
        Verifies that a text with count_tokens == (context_window - reserved_output_tokens + 1)
        returns False from fits_in_budget (one token over the adjusted limit).
        A text with count_tokens == (context_window - reserved_output_tokens) returns True.
    """
    context_window, reserved = 100, 60
    available = context_window - reserved  # 40 tokens

    exactly_at_limit = "x" * (available * 4)  # char_heuristic: 40 tokens
    one_token_over = "x" * ((available + 1) * 4)  # 41 tokens

    assert fits_in_budget(exactly_at_limit, context_window, reserved, "char_heuristic")
    assert not fits_in_budget(one_token_over, context_window, reserved, "char_heuristic")


def test_hf_auto_family_skips_if_not_installed():
    """
    Assert that count_tokens with family="hf_auto" raises ImportError if transformers absent.

    Purpose:
        Verifies graceful failure when the [hf-tokenizer] optional extra is not installed.
        Skips if transformers IS installed (so environments with the extra don't break).
    """
    try:
        import transformers  # noqa: F401

        pytest.skip("transformers installed — guarded-import failure path not testable.")
    except ImportError:
        pass

    with pytest.raises(ImportError) as exc_info:
        count_tokens("some text", "hf_auto", model_name="gpt2")
    # The error must be actionable, naming the extra and the config alternative.
    assert "hf-tokenizer" in str(exc_info.value)
    assert "tokenizer_family" in str(exc_info.value)


def test_unknown_family_raises_value_error():
    """
    Assert count_tokens raises ValueError (not a silent fallback) for a bad family.

    Purpose:
        A misconfigured tokenizer_family must fail loudly — silently falling back to
        char_heuristic would corrupt token budget math without any signal.
    """
    with pytest.raises(ValueError):
        count_tokens("text", "not_a_family")
