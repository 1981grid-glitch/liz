import re

_SENTENCE_ENDS = (". ", "! ", "? ", ".\n", "!\n", "?\n")

_MD_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_MD_ITALIC = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")
_MD_CODE = re.compile(r"`([^`]+)`")
_MD_HEADER = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD_BULLET = re.compile(r"^[\s]*[-*]\s+", re.MULTILINE)


class IncrementalSentenceSplitter:
    """Feed streaming text deltas in; get complete sentences out as their
    boundary arrives, so TTS can start on sentence 1 while the model is
    still generating sentence 2."""

    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, delta: str) -> list[str]:
        self._buffer += delta
        sentences = []
        while True:
            earliest = len(self._buffer)
            for sep in _SENTENCE_ENDS:
                idx = self._buffer.find(sep)
                if idx != -1:
                    end = idx + len(sep)
                    if end < earliest:
                        earliest = end
            if earliest == len(self._buffer):
                break
            sentence = self._buffer[:earliest].strip()
            self._buffer = self._buffer[earliest:]
            if sentence:
                sentences.append(sentence)
        return sentences

    def flush(self) -> str:
        remainder = self._buffer.strip()
        self._buffer = ""
        return remainder


def clean_for_speech(text: str) -> str:
    text = _MD_LINK.sub(r"\1", text)
    text = _MD_BOLD.sub(r"\1", text)
    text = _MD_ITALIC.sub(r"\1", text)
    text = _MD_CODE.sub(r"\1", text)
    text = _MD_HEADER.sub("", text)
    text = _MD_BULLET.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text
