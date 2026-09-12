"""Entry point for seeding the provider-owned database.

Separate from app.seed because the two databases have separate owners: this runs
inside the provider service, which is the only process with a route to them.
"""

from .seed import seed_providers

if __name__ == "__main__":
    seed_providers()
    print("provider database seeded")
