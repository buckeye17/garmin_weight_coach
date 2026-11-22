"""Quick reminders for riding out a flare: what to cut back on, what helps, and how to
look after the joint. General, well-established advice (ACR and NHS guidance), shown
while a flare is going on; it doesn't replace what a doctor has said.
"""

# (heading, icon, color, tips)
FLARE_TIPS: list[tuple[str, str, str, list[str]]] = [
    (
        "Ease off",
        "tabler:circle-minus",
        "red",
        [
            "Red meat, organ meats, gravy and meat broth",
            "Shellfish and small oily fish: anchovies, sardines, mackerel, herring",
            "Alcohol, beer most of all",
            "Sugary drinks, fruit juice and sweets: fructose raises uric acid",
        ],
    ),
    (
        "Lean on",
        "tabler:circle-plus",
        "teal",
        [
            "Water: aim for 2–3 litres (8–12 glasses) through the day",
            "Low-fat milk and yogurt",
            "Cherries or unsweetened tart cherry juice",
            "Vegetables, whole grains, eggs, beans and lentils",
            "Coffee, if you already drink it",
        ],
    ),
    (
        "Look after the joint",
        "tabler:first-aid-kit",
        "blue",
        [
            "Rest it and keep it raised",
            "Ice wrapped in a cloth, 15–20 minutes at a time",
            "Take flare medicine early and as prescribed",
            "Keep taking daily uric acid medicine (like allopurinol) unless told otherwise",
            "Don't diet hard or fast until it settles: quick weight loss raises uric acid",
            "Skip hard workouts and saunas: dehydration makes it worse",
        ],
    ),
]
