"""Shared help text for BOM estimator UI surfaces.

Keep this text centralized so main-window and settings help dialogs stay in
sync and reviewers can validate wording in one place.
"""

BOM_ESTIMATOR_HELP_TITLE = "BOM estimator help"

BOM_ESTIMATOR_HELP_TEXT = (
    "BOM estimator notes:\n\n"
    "Boards vs Assemble\n"
    "• Boards is how many bare PCBs are ordered — JLC's minimum is five.\n"
    "• Assemble is how many of them get populated, and may be fewer.\n"
    "• Components and per-joint assembly are charged for the boards that are "
    "assembled; setup, stencil, THT setup and the extended-part fee are "
    "charged once per order whichever it is.\n"
    "• The per-board figure is per *assembled* board. The bare ones carry "
    "none of this.\n\n"
    "What is counted\n"
    "• Component prices come from JLC's quantity ladder for the whole order, "
    "so a part used on twenty references is priced at twenty times the "
    "assembled quantity, not at one board's worth.\n"
    "• Rows excluded from the BOM, and rows KiCad marks DNP, are left out "
    "entirely. A row excluded only from the position files still costs its "
    "components — it is still in the BOM you send — but no assembly.\n"
    "• Missing prices are named in the summary. Any part nobody could price "
    "contributes nothing, so the total is a floor rather than an estimate.\n\n"
    "What is NOT counted\n"
    "• PCB fabrication, shipping, tax and customs. This prices the BOM and "
    "the assembly, not the order.\n"
    "• Parts attrition. JLC adds spare components to cover placement loss and "
    "charges for them; the rule is not published in a form worth guessing at, "
    "so small runs will come out under.\n"
    "• Minimum purchase quantities on individual parts.\n\n"
    "• Uses live network requests to look up some part metadata in real time.\n"
    "• Values shown are rough estimates for planning and comparison only.\n"
    "• Final pricing is always provided by JLC at order time.\n"
    "• If you see serious inconsistencies, please report an issue with details."
)


def get_bom_estimator_help_text() -> str:
    """Return BOM estimator help text used by all UI help popups."""
    return BOM_ESTIMATOR_HELP_TEXT
