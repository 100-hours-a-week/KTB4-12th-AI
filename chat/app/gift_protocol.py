"""Single explicit protocol registry for web, replay, supervision and evaluation."""
import hashlib
import os
from pathlib import Path

DEFAULT = 'stable-v4.12'


def harness_class(version=None):
    from app.gift_adapter import PublicHarness
    from app.gift_guided import PromptHarness, GuidedHarness
    from app.gift_stable import StableHarness, StableV31Harness, StableV4Harness
    from app.gift_validated import ValidatedHarness, ThreeGiftHarness
    from app.gift_browse import BrowseHarness
    from app.gift_cached import CachedCardHarness
    from app.gift_gallery import GalleryHarness
    from app.gift_consult import ConsultHarness
    from app.gift_dialogue import DialogueHarness
    from app.gift_simple import SimpleHarness
    from app.gift_recipient_categories import RecipientCategoryHarness
    from app.gift_compact import CompactHarness
    from app.gift_clean_io import CleanIOHarness
    from app.gift_conversation import ConversationHarness
    registry = {'baseline': PublicHarness, 'baseline-v1': PublicHarness,
                'prompt': PromptHarness, 'prompt-v1': PromptHarness,
                'guided': GuidedHarness, 'guided-v2': GuidedHarness, 'stable-v3': StableHarness, 'stable-v3.1': StableV31Harness,
                'stable-v4': StableV4Harness, 'stable-v4.1': ValidatedHarness, 'stable-v4.2': ThreeGiftHarness, 'stable-v4.3': BrowseHarness, 'stable-v4.4': CachedCardHarness, 'stable-v4.5': GalleryHarness, 'stable-v4.6': ConsultHarness, 'stable-v4.7': DialogueHarness, 'stable-v4.8': SimpleHarness}
    registry['stable-v4.9'] = RecipientCategoryHarness
    registry['stable-v4.10'] = CompactHarness
    registry['stable-v4.11'] = CleanIOHarness
    registry['stable-v4.12'] = ConversationHarness
    name = version if version is not None else os.getenv('GIFT_HARNESS', DEFAULT)
    if name not in registry:
        raise ValueError('Unknown GIFT_HARNESS version: ' + str(name))
    return registry[name]


def manifest(version=None):
    cls = harness_class(version)
    root = Path(__file__).parent
    files = ['gift_protocol.py', 'gift_stable.py', 'gift_validated.py', 'gift_browse.py', 'gift_cached.py', 'gift_gallery.py', 'gift_consult.py', 'gift_guided.py', 'gift_adapter.py', 'conversation_memory.py',
             'gift_core/harness.py', 'gift_core/contracts.py', 'search_models.py', 'models.py']
    if getattr(cls, 'variant', None) == 'stable-v4.7':
        files.append('gift_dialogue.py')
    if getattr(cls, 'variant', None) in ('stable-v4.8', 'stable-v4.9', 'stable-v4.10', 'stable-v4.11', 'stable-v4.12'):
        files.append('gift_simple.py')
    if getattr(cls, 'variant', None) in ('stable-v4.9', 'stable-v4.10', 'stable-v4.11', 'stable-v4.12'):
        files.append('gift_recipient_categories.py')
    if getattr(cls, 'variant', None) in ('stable-v4.10', 'stable-v4.11', 'stable-v4.12'):
        files.append('gift_compact.py')
    if getattr(cls, 'variant', None) in ('stable-v4.11', 'stable-v4.12'):
        files.append('gift_clean_io.py')
    if getattr(cls, 'variant', None) == 'stable-v4.12':
        files.append('gift_conversation.py')
    hashes = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in files}
    search_root = root.parent.parent / 'search/search'
    retrieval = {name:hashlib.sha256((search_root/name).read_bytes()).hexdigest()
                 for name in ('models.py','service.py') if (search_root/name).is_file()}
    digest = hashlib.sha256(''.join(name + hashes[name] for name in sorted(hashes)).encode()).hexdigest()
    return {'version': getattr(cls, 'variant', 'baseline-v1'), 'sourceSha256': digest,
            'sourceHashes': hashes, 'retrievalSourceHashes': retrieval, 'serverTextIsTrainingTarget': False,
            'wireFormat': 'provider-native; same logical function schemas'}
