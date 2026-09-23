"""The grid emission factor a site's CUE is computed against.

Revision ID: 0073
Revises: 0072

CUE is total facility energy times a grid carbon intensity, divided by IT
energy. The platform meters both energies already; what it has never had is the
intensity, and that is not something a datacenter can measure. It comes from
outside, and there are exactly two honest ways to get it.

**An annual published factor**, which is what nearly every operator reports
against. In the US that is EPA eGRID: a subregion total-output CO2e rate,
refreshed when EPA publishes, applied flat across the year. It is coarse - it
cannot tell 3 a.m. wind from 6 p.m. gas - but it is auditable, it is what a
sustainability report is expected to cite, and two sites computed against it
can be compared.

**An hourly marginal or average feed** from Electricity Maps or WattTime, which
is what a carbon-aware workload scheduler needs. That is a live integration
with a credential and a failure mode, and it answers a different question.

This seeds the first. The factor lives on the datacenter row rather than in
code for the reason every reference number should: eGRID is re-published,
sites move utility, and a figure that ends up in a public report must be
changeable by the people who own the report without a deploy. `grid_carbon_basis`
travels beside it so the drawer can name the source next to the number - an
unattributed carbon figure is worse than none.

THE FIGURES BELOW ARE eGRID ANNUAL TOTAL-OUTPUT CO2e RATES, AS SHIPPED
DEFAULTS, AND SHOULD BE RE-CHECKED AGAINST THE CURRENT eGRID RELEASE BEFORE
ANYTHING PUBLISHES THEM. DC1 (Chicago) sits in subregion RFCW and DC2 (New
York City) in NYCW. They are seeded so the tile is live and its arithmetic is
visible; they are not a substitute for the site's own utility disclosure,
which is what a real operator would use where one exists.
"""

from alembic import op

revision = "0073"
down_revision = "0072"
branch_labels = None
depends_on = None

# subregion -> (kg CO2e / kWh, how it is described on the page)
_SEED = {
    "DC1": ("0.441", "eGRID subregion RFCW annual total output rate"),
    "DC2": ("0.272", "eGRID subregion NYCW annual total output rate"),
}


def upgrade() -> None:
    for code, (factor, basis) in _SEED.items():
        op.execute(f"""
            UPDATE datacenter
               SET attributes = COALESCE(attributes, '{{}}'::jsonb) || jsonb_build_object(
                     'grid_carbon_kg_per_kwh', {factor}::numeric,
                     'grid_carbon_basis', '{basis}',
                     'grid_carbon_source', 'published_factor')
             WHERE code = '{code}'
               -- Never overwrite a factor somebody has already set: a site that
               -- has its utility's own disclosure loaded should keep it.
               AND NOT (COALESCE(attributes, '{{}}'::jsonb) ? 'grid_carbon_kg_per_kwh')
        """)


def downgrade() -> None:
    op.execute("""
        UPDATE datacenter
           SET attributes = (COALESCE(attributes, '{}'::jsonb)
                             - 'grid_carbon_kg_per_kwh'
                             - 'grid_carbon_basis'
                             - 'grid_carbon_source')
         WHERE attributes ->> 'grid_carbon_source' = 'published_factor'
    """)
