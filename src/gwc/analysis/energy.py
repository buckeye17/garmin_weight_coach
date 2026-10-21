"""Energy density of weight change (Forbes).

Instead of a flat 3,500 kcal/lb, the share of a change that is fat depends on
how much fat there is: fat fraction = FM / (FM + 10.4 kg). Fat tissue stores
~9,440 kcal/kg and lean tissue ~1,816 kcal/kg.
"""

FORBES_C_KG = 10.4
FAT_KCAL_PER_KG = 9440.0
LEAN_KCAL_PER_KG = 1816.0
# Used when the scale has never reported body fat.
DEFAULT_BODY_FAT_PCT = 30.0
DAYS_PER_MONTH = 30.44


def fat_fraction(fat_kg: float) -> float:
    fat_kg = max(fat_kg, 0.0)
    return fat_kg / (fat_kg + FORBES_C_KG)


def rho(fat_kg: float) -> float:
    """kcal per kg of weight change at this fat mass."""
    f = fat_fraction(fat_kg)
    return f * FAT_KCAL_PER_KG + (1 - f) * LEAN_KCAL_PER_KG
