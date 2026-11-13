"""Add Hub feedback storage without changing or importing Airtable records."""
from sqlalchemy import inspect, UniqueConstraint
from app.db_v2.database import engine_v2
from app.db_v2.models.hub_feedback import HubFeedbackV2, BotFeedbackV2


def main():
    for model in (HubFeedbackV2, BotFeedbackV2):
        model.__table__.create(bind=engine_v2, checkfirst=True)
        inspector = inspect(engine_v2)
        columns = {column["name"]: column for column in inspector.get_columns(model.__tablename__)}
        if set(model.__table__.columns.keys()) - set(columns):
            raise RuntimeError(f"{model.__tablename__} has an older schema; review an additive upgrade before deployment.")
        if any(column.nullable != columns[column.name]["nullable"] for column in model.__table__.columns):
            raise RuntimeError(f"{model.__tablename__} has incompatible column nullability; review the schema before deployment.")
        uniques = {tuple(value["column_names"]) for value in inspector.get_unique_constraints(model.__tablename__)}
        expected = {tuple(column.name for column in constraint.columns) for constraint in model.__table__.constraints if isinstance(constraint, UniqueConstraint)}
        if not expected.issubset(uniques):
            raise RuntimeError(f"{model.__tablename__} is missing required uniqueness constraints.")
        for index in model.__table__.indexes:
            index.create(bind=engine_v2, checkfirst=True)
    print("Hub and Bot feedback tables and indexes are ready.")


if __name__ == "__main__":
    main()
