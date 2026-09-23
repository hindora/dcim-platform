"""The design PUE each site's measured PUE is read against.

Revision ID: 0074
Revises: 0073

`datacenter.design_pue` has existed in the schema since the data model was
written and has been NULL on every row, so the site panel showed a measured
PUE with nothing to judge it by. 1.31 is a number; "1.31 against a 1.35
design" is a verdict.

WHERE THE FIGURE COMES FROM. Not invented and not an industry rule of thumb:
it is the simulated plant's own calibration anchor, in
core/cooling_model.py of the simulator, where PUE at the design point is
defined as 1 + OH_FLOOR + OH_VAR with OH_FLOOR 0.15 and OH_VAR derived from
the rated chiller COP of 5.5. That evaluates to 1.354 at the reference
condition - 15 C ambient, design IT load. A real site would take this figure
from its commissioning report or its design intent document instead, and the
basis string is stored beside the number so whoever replaces it can see what
it replaced.

THE TWO FIGURES ARE NOT THE SAME DEFINITION, AND THE PANEL SAYS SO. The
anchor above is COOLING-ONLY - that module states plainly that UPS and PDU
losses are not modelled in it. The platform's measured PUE is Green Grid
Category 1: total facility energy at the utility feed over IT energy at the
UPS output, which contains the cooling plant AND the distribution losses
between the two. So the measured figure includes overheads the design figure
never counted, and at equal conditions it should read HIGHER.

That is why this seeds context and not a pass mark. The tile shows the design
figure and the gap, and the hover says what the gap does and does not mean; it
does NOT colour the metric green for beating a target it is not measured
against. Colouring it would be the same error as grading a generator room on
ASHRAE: a number compared with something that is not its like.
"""

from alembic import op

revision = "0074"
down_revision = "0073"
branch_labels = None
depends_on = None

_DESIGN_PUE = "1.354"
_BASIS = ("plant calibration anchor: 1 + OH_FLOOR + OH_VAR at 15 C ambient "
          "and design IT load, cooling-only")


def upgrade() -> None:
    # Never overwrite a figure somebody has already entered: a site with its
    # commissioning PUE loaded should keep it.
    op.execute(f"""
        UPDATE datacenter
           SET design_pue = {_DESIGN_PUE},
               attributes = COALESCE(attributes, '{{}}'::jsonb) || jsonb_build_object(
                     'design_pue_basis', '{_BASIS}',
                     'design_pue_source', 'plant_model_anchor')
         WHERE design_pue IS NULL
    """)


def downgrade() -> None:
    op.execute("""
        UPDATE datacenter
           SET design_pue = NULL,
               attributes = (COALESCE(attributes, '{}'::jsonb)
                             - 'design_pue_basis' - 'design_pue_source')
         WHERE attributes ->> 'design_pue_source' = 'plant_model_anchor'
    """)
