"""Fixed token-window-mean-v1 identity and lossless original-text windows."""

from collections.abc import Callable, Iterator

WINDOW_PROFILE = "token-window-mean-v1"
TOKEN_LIMIT = 512
WINDOW_BATCH_SIZE = 8


def parse_embedding_model_ref(model_ref: str) -> tuple[str, bool]:
    """Separate logical profile identity from the actual local model path."""
    if not isinstance(model_ref, str) or not model_ref.strip():
        raise ValueError("embedding model reference must be nonempty")
    if "#" not in model_ref:
        return model_ref, False
    path, separator, profile = model_ref.partition("#")
    if not path.strip() or not separator or profile != WINDOW_PROFILE:
        raise ValueError("unsupported embedding profile")
    return path, True


def token_windows(text: str, count_tokens: Callable[[str], int]) -> Iterator[str]:
    """Yield non-overlapping original spans, each including <=512 model tokens.

    Fitting inputs remain whole. For longer inputs v1 searches safe prefixes of
    at most 512 Python characters, accepting only measured fitting prefixes.
    Token counts need not be monotonic: binary search may underfill a window,
    but every emitted span is checked and all original characters are retained.
    No tokenizer offsets or decode roundtrip can discard whitespace/Unicode.
    """

    def fits(piece: str) -> bool:
        count = count_tokens(piece)
        if type(count) is not int or count < 1:
            raise ValueError("invalid embedding token count")
        return count <= TOKEN_LIMIT

    if not text:
        raise ValueError("embedding input must be nonempty")
    if fits(text):
        yield text
        return
    start = 0
    while start < len(text):
        low, high, accepted = 1, min(TOKEN_LIMIT, len(text) - start), 0
        while low <= high:
            middle = (low + high) // 2
            if fits(text[start : start + middle]):
                accepted, low = middle, middle + 1
            else:
                high = middle - 1
        if not accepted:
            raise ValueError("embedding character exceeds token budget")
        piece = text[start : start + accepted]
        if not fits(piece):
            raise ValueError("embedding tokenizer is inconsistent")
        yield piece
        start += accepted
