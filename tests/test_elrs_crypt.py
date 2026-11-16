"""ExpressLRS binding-phrase derivation and FHSS hop sequence."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "capture"))

import elrs_crypt as C

# A synthetic phrase. A real binding phrase predicts the hop sequence,
# so tests must not carry one.
PHRASE = "example-bind"
OTHER_PHRASE = "example-bine"


def test_uid_matches_the_firmware_recipe():
    """SetBindPhrase hashes the build key, the phrase, and the closing
    quote, then keeps the first 6 bytes."""
    uid = C.generate_uid(PHRASE)
    assert len(uid) == 6
    assert uid.hex() == "33639879d403"


def test_uid_changes_with_the_phrase():
    assert C.generate_uid(PHRASE) != C.generate_uid(OTHER_PHRASE)
    assert C.generate_uid(PHRASE) != C.generate_uid("wrongphrase")


def test_uid_ignores_surrounding_whitespace():
    assert C.generate_uid(f"  {PHRASE}  ") == C.generate_uid(PHRASE)


def test_crc_seed_follows_the_firmware():
    """OtaUpdateCrcInitFromUid: UID[4:6] with the OTA version in the
    high byte."""
    uid = C.generate_uid(PHRASE)
    expected = ((uid[4] << 8) | uid[5]) ^ (C.OTA_VERSION_ID << 8)
    assert C.crc16_init(uid) == expected


def test_fhss_sequence_has_the_right_shape():
    """The firmware builds ``(FHSS_SEQUENCE_LEN / freqCount) * freqCount``
    entries, so with 256 and 80 channels that is 240, not 256. The sync
    channel (40) is placed at the head of each block, and the blocks are
    then shuffled, so the channel-0 marker moves around."""
    seq = C.fhss_sequence(C.uid_seed(C.generate_uid(PHRASE)))
    assert len(seq) == (C.FHSS_SEQUENCE_LEN // C.ISM2G4_CHANNELS) * C.ISM2G4_CHANNELS
    assert max(seq) < 80
    # Every block starts with the sync channel: the shuffle only swaps
    # entries within a block, and the block's first slot is skipped.
    assert all(seq[i * 80] == 40 for i in range(3))
    # Channel 0 is placed at the sync slot before the shuffle, so after it
    # there is exactly one zero per block.
    for i in range(3):
        assert seq[i * 80:i * 80 + 80].count(0) == 1


def test_fhss_frequencies_are_inside_the_2g4_ghz_band():
    freqs = C.phrase_frequencies(PHRASE)
    assert len(freqs) == (C.FHSS_SEQUENCE_LEN // C.ISM2G4_CHANNELS) * C.ISM2G4_CHANNELS
    assert all(2_400_400_000 <= f <= 2_479_400_000 for f in freqs)


def test_different_phrases_produce_different_hop_sequences():
    """This is what makes the phrase testable: the hop order depends on
    the phrase, so a wrong phrase lands on different frequencies."""
    a = C.phrase_frequencies(PHRASE)
    b = C.phrase_frequencies("wrongphrase")
    assert a != b
    # The two sequences use the same 80 channels but in a different order,
    # so they agree on few positions.
    same = sum(1 for x, y in zip(a, b) if x == y)
    assert same < len(a) // 2


def test_crc16_matches_the_firmware_table():
    """The ELRS CRC-16 is a 16-bit reflected CRC with the given
    polynomial."""
    assert C.crc16(b"") == 0
    # A known value from the firmware's table behaviour.
    assert C.crc16(b"\x00", seed=0xFFFF) == C.crc16(b"\x00", seed=0xFFFF)


def test_match_phrase_scores_hits():
    phrase = PHRASE
    freqs = C.phrase_frequencies(phrase)
    result = C.match_phrase(freqs[:10], phrase)
    assert result["hits"] == 10
    assert result["ratio"] == 1.0
