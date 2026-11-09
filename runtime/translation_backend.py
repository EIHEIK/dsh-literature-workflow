"""Provider-neutral translation contract. Qwen-MT remains deliberately disabled."""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class TranslationInput:
    segment_id: str
    source: str
    source_language: str = 'en'
    target_language: str = 'zh-CN'
    context: tuple = field(default_factory=tuple)
    terminology: tuple = field(default_factory=tuple)
    translation_memory: tuple = field(default_factory=tuple)
    background_hash: str = ''


@dataclass(frozen=True)
class TranslationOutput:
    segment_id: str
    translation: str
    provider: str
    model: str
    request_id: str
    usage: dict
    finish_reason: str

    def validate(self, source):
        if self.segment_id != source.segment_id or self.finish_reason != 'stop' or not self.translation.strip():
            raise ValueError('Incomplete or mismatched translation response')
        if not self.provider or not self.model or not self.request_id or not isinstance(self.usage, dict):
            raise ValueError('Translation provenance and usage required')
        return self


class QwenMT:
    provider = 'qwen-mt'
    enabled = False

    def translate(self, request: TranslationInput) -> TranslationOutput:
        raise RuntimeError('Qwen-MT is an interface placeholder; no credentials, network requests or fallback are enabled')
