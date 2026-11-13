"""The admin floor, plus RBAC audit findings §2.2 and §2.3
(session_handoff_2026-08-25-rank-to-depth.md §2.1, §2.2, §2.3;
plan_access_control_algorithm_2026-08-27.md §6.5, §6.8).

Four independent pieces of work, one module:

  1. §2.2 — the hub-user search wildcard bypass.
  2. §2.3 — `/me/revocable`'s per-row closure rebuilds.
  3. §2.1 — `is_system` becomes settable (by script) and stays unclearable.
  4. The last-admin guard — the final `(hub_admin, universal_scope)`
     assignment cannot be revoked, BY ANYONE.

Fixture is `test_rbac_api.py`'s, INCLUDING its `finally: db.close()`
dependency override rather than the one-line one the other assignment tests
use. Nothing here asserts on rollback, but step 6 recorded that the leak
makes a missing `db.rollback()` invisible through the router, and copying
the corrected version costs nothing.
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone

import jwt
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg2://validation:validation@localhost/validation",
)
os.environ.setdefault(
    "DATABASE_URL_V2",
    "postgresql+psycopg2://validation:validation@localhost/validation_v2",
)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-admin-floor-tests-only")
os.environ.setdefault("JWT_ALGORITHM", "HS256")


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs):
    return "JSON"


from app.db_v2.database import BaseV2, get_db_v2  # noqa: E402
from app.db_v2.models.user import UserV2  # noqa: E402
from app.db_v2.models.role import RoleEdgeV2, RoleV2  # noqa: E402
from app.db_v2.models.role_assignment import RoleAssignmentV2  # noqa: E402
from app.db_v2.models.scope import ScopeEdgeV2, ScopeV2  # noqa: E402
from app.main import create_app  # noqa: E402

HUB_ADMIN = "Hub Admin"
ADMIN_EMAIL = "admin@renphil.org"


class AdminFloorFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )

        @event.listens_for(self.engine, "connect")
        def _enable_fk(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        BaseV2.metadata.create_all(self.engine)
        self._session_factory = sessionmaker(bind=self.engine)
        self.db = self._session_factory()

        app = create_app()

        # test_rbac_api.py's override, verbatim — see this module's docstring.
        def _override_db():
            db = self._session_factory()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db_v2] = _override_db
        self.client = TestClient(app, raise_server_exceptions=False)

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    # -- auth -----------------------------------------------------------

    def _auth(self, email: str, roles: list[str] | None = None) -> dict:
        token = jwt.encode(
            {
                "sub": email,
                "name": email.split("@")[0],
                "roles": roles or [],
                "exp": datetime.now(timezone.utc) + timedelta(minutes=30),
            },
            os.environ["JWT_SECRET_KEY"],
            algorithm=os.environ["JWT_ALGORITHM"],
        )
        return {"Authorization": f"Bearer {token}"}

    @property
    def admin(self) -> dict:
        self._ensure_admin()
        return self._auth(ADMIN_EMAIL, [HUB_ADMIN])

    def user(self, email: str) -> dict:
        return self._auth(email, [])

    def _ensure_admin(self) -> None:
        """Since phase 3 the JWT "Hub Admin" claim makes nobody an admin, so
        the admin is a real `(hub_admin, all_scopes)` row, seeded straight
        into the database (the API can't build it: only an admin may create
        roles). Lazy, so tests that never act as admin seed nothing.

        THIS ROW COUNTS TOWARDS THE FLOOR. Every caller of the admin-only
        paths now holds one, so a test about "the last admin" revokes this
        row, and a test that needs zero admins drops it with
        `_drop_admin_assignment` after building what it needs."""
        if getattr(self, "admin_assignment_id", None) is not None:
            return
        now = datetime.now(timezone.utc)
        role = RoleV2(key="hub_admin", name="Hub Admin", is_public=False, is_system=False, created_at=now)
        scope = ScopeV2(
            key="all_scopes",
            name="All Scopes",
            is_universal=True,
            is_public=False,
            is_system=False,
            created_at=now,
        )
        user = UserV2(work_email=ADMIN_EMAIL, name="admin", has_signed_in=True, created_at=now, updated_at=now)
        self.db.add_all([role, scope, user])
        self.db.flush()
        assignment = RoleAssignmentV2(user_id=user.id, role_id=role.id, scope_id=scope.id, created_at=now)
        self.db.add(assignment)
        self.db.commit()
        self.hub_admin_role = role.id
        self.all_scopes = scope.id
        self.admin_assignment_id = assignment.id

    def _drop_admin_assignment(self) -> None:
        """Back to a hub with zero admin rows. `self.admin` still signs in,
        but is an ordinary user from here on."""
        self.db.query(RoleAssignmentV2).filter(RoleAssignmentV2.id == self.admin_assignment_id).delete()
        self.db.commit()

    # -- builders -------------------------------------------------------

    def make_role(self, key: str, name: str, public: bool = False) -> int:
        body: dict = {"key": key, "name": name}
        if public:
            body["is_public"] = True
        res = self.client.post("/v2/rbac/roles", json=body, headers=self.admin)
        self.assertEqual(res.status_code, 201, res.text)
        return res.json()["data"]["id"]

    def make_scope(
        self, key: str, name: str, universal: bool = False, public: bool = False
    ) -> int:
        body: dict = {"key": key, "name": name, "is_universal": universal}
        if public:
            body["is_public"] = True
        res = self.client.post("/v2/rbac/scopes", json=body, headers=self.admin)
        self.assertEqual(res.status_code, 201, res.text)
        return res.json()["data"]["id"]

    def add_role_edge(self, parent_id: int, child_id: int) -> None:
        res = self.client.post(
            f"/v2/rbac/roles/{parent_id}/children/{child_id}", headers=self.admin
        )
        self.assertEqual(res.status_code, 204, res.text)

    def add_scope_edge(self, parent_id: int, child_id: int) -> None:
        res = self.client.post(
            f"/v2/rbac/scopes/{parent_id}/children/{child_id}", headers=self.admin
        )
        self.assertEqual(res.status_code, 204, res.text)

    def provision(self, headers: dict) -> None:
        res = self.client.get("/v2/rbac/me/assignments", headers=headers)
        self.assertEqual(res.status_code, 200, res.text)

    def grant(self, granter_headers: dict, target_email: str, role_id: int, scope_id: int) -> dict:
        res = self.client.post(
            "/v2/rbac/assignments",
            json={"user_email": target_email, "role_id": role_id, "scope_id": scope_id},
            headers=granter_headers,
        )
        self.assertEqual(res.status_code, 201, res.text)
        return res.json()["data"]

    def code_of(self, res) -> str:
        return res.json()["detail"]["code"]

    def build_org(self) -> None:
        """The same shape `test_rbac_assignments_api.build_org` uses, so a
        reader moving between the two files is looking at one org. Hub Admin
        and All Scopes come from `_ensure_admin`."""
        self._ensure_admin()
        self.program_lead = self.make_role("program_lead", "Program Lead")
        self.function_lead = self.make_role("function_lead", "Function Lead")
        self.program_member = self.make_role("program_member", "Program Member")
        self.function_member = self.make_role("function_member", "Function Member")
        self.hub_member = self.make_role("hub_member", "Hub Member")
        self.add_role_edge(self.hub_admin_role, self.program_lead)
        self.add_role_edge(self.hub_admin_role, self.function_lead)
        self.add_role_edge(self.program_lead, self.program_member)
        self.add_role_edge(self.function_lead, self.function_member)
        self.add_role_edge(self.program_member, self.hub_member)
        self.add_role_edge(self.function_member, self.hub_member)

        self.scope_a = self.make_scope("scope_a", "Scope A")
        self.scope_b = self.make_scope("scope_b", "Scope B")


# =========================================================================
# §2.2 — the hub-user search privacy control is bypassable
#
# THE BYPASS IS REPRODUCED FIRST, in TestTheWildcardBypass, against the
# UNFIXED code. A test written after a fix cannot demonstrate the hole was
# ever open, and this one was: `%%%` and `___` each satisfy the router's
# `min_length=3` and each returned the entire directory, 20 at a time.
# =========================================================================


class SearchFixture(AdminFloorFixture):
    """Twelve people, deliberately sharing no common substring, so a search
    returning ALL of them can only be a wildcard match and never a lucky
    prefix. Two of them carry a LITERAL underscore — one in the email, one in
    the name — which is what proves the escape matches literally rather than
    as a wildcard.
    """

    PEOPLE = [
        ("alice@renphil.org", "Alice Adams"),
        ("bob@renphil.org", "Bob Brown"),
        ("carol@renphil.org", "Carol Clark"),
        ("dan@renphil.org", "Dan Diaz"),
        ("erin@renphil.org", "Erin Evans"),
        ("frank@renphil.org", "Frank Fox"),
        ("grace@renphil.org", "Grace Green"),
        ("hugo@renphil.org", "Hugo Hall"),
        ("ivy@renphil.org", "Ivy Ives"),
        ("jon@renphil.org", "Jon Jones"),
        # The literal-metacharacter rows: two underscores and one backslash.
        ("q_marks@renphil.org", "Quinn Marks"),
        ("rita@renphil.org", "Rita O_Neil"),
        ("sam@renphil.org", "Sam A\\B"),
    ]

    def setUp(self) -> None:
        super().setUp()
        now = datetime.now(timezone.utc)
        for email, name in self.PEOPLE:
            self.db.add(UserV2(email=email, name=name, has_signed_in=True, created_at=now))
        self.db.commit()

    def search(self, q: str):
        res = self.client.get("/v2/rbac/hub-users", params={"q": q}, headers=self.admin)
        self.assertEqual(res.status_code, 200, res.text)
        return [row["email"] for row in res.json()["data"]]


class TestTheWildcardBypass(SearchFixture):
    """§5.4 decided this endpoint requires a real search prefix rather than
    being an org-wide people dump. These are the inputs that defeated it."""

    def test_percent_wildcards_no_longer_dump_the_directory(self) -> None:
        """`%%%` — three characters, so it clears `min_length=3`, and each
        one is a LIKE wildcard. Before the fix this returned every row."""
        self.assertEqual(self.search("%%%"), [])

    def test_underscore_wildcards_no_longer_dump_the_directory(self) -> None:
        """`___` — the single-character wildcard, three times. Matches any
        three-character run, i.e. every email in existence. Before the fix
        this returned every row."""
        self.assertEqual(self.search("___"), [])

    def test_a_single_percent_padded_to_length_still_matches_nothing(self) -> None:
        """`a%b` reads as "starts with a, ends with b" and would sweep up
        most of the directory. Nobody has a literal `a%b` in their name."""
        self.assertEqual(self.search("a%b"), [])

    def test_the_directory_is_not_reachable_by_any_metacharacter_probe(self) -> None:
        """Quantified rather than case-by-case: no wildcard-only query may
        return more rows than a real prefix search does. The bar is
        deliberately "fewer than everyone" — an escaped `___` legitimately
        matches `q_marks` on nothing, but a future change that made one of
        these match a handful should not silently become a dump."""
        everyone = len(self.PEOPLE)
        for probe in ("%%%", "___", "%_%", "_%_", "a%b", "%o%", "___%"):
            with self.subTest(probe=probe):
                self.assertLess(len(self.search(probe)), everyone)


class TestLegitimateSearchStillWorks(SearchFixture):
    """The other half. An escape that broke ordinary search would "fix" §2.2
    by making the endpoint useless."""

    def test_an_ordinary_prefix_still_matches(self) -> None:
        self.assertEqual(self.search("ali"), ["alice@renphil.org"])

    def test_an_infix_still_matches(self) -> None:
        """The pattern is `%term%`, not `term%` — substring search is the
        behaviour this endpoint has always had and is not being narrowed."""
        self.assertEqual(self.search("race"), ["grace@renphil.org"])

    def test_it_still_matches_on_name_as_well_as_email(self) -> None:
        self.assertEqual(self.search("Diaz"), ["dan@renphil.org"])

    def test_it_is_still_case_insensitive(self) -> None:
        self.assertEqual(self.search("ALICE"), ["alice@renphil.org"])

    def test_a_query_matching_several_people_returns_them_all(self) -> None:
        """A superset check, not equality: `admin@renphil.org` is
        auto-provisioned by the builder calls in `setUp`, so the directory is
        the twelve PEOPLE plus whoever else has hit an endpoint."""
        found = set(self.search("renphil.org"))
        self.assertTrue(found.issuperset({email for email, _ in self.PEOPLE}))


class TestALiteralUnderscoreMatchesLiterally(SearchFixture):
    """The escape must make `_` mean `_`, not "any character". Without this
    pair of tests a fix that simply STRIPPED metacharacters would pass
    everything above while quietly making two real people unfindable."""

    def test_a_literal_underscore_in_an_email_is_findable(self) -> None:
        self.assertEqual(self.search("q_marks"), ["q_marks@renphil.org"])

    def test_a_literal_underscore_in_a_name_is_findable(self) -> None:
        self.assertEqual(self.search("O_Neil"), ["rita@renphil.org"])

    def test_the_underscore_does_not_act_as_a_wildcard(self) -> None:
        """`q_m` matches `q_marks`. `qXm` must not — under an unescaped
        pattern `_` would match any character, so a test that only checked
        `q_marks` finds itself would pass either way.

        THIS IS ALSO WHAT PINS THE ESCAPE ORDER. Doubling the backslash
        SECOND turns `q\\_m` into `q\\\\_m`, i.e. "q, literal backslash,
        literal underscore, m" — which matches nobody, so the first assertion
        below goes red. The wrong order does not reopen the §2.2 hole (it
        over-escapes rather than under-escapes); it silently makes people
        with a metacharacter in their address unfindable instead, which is
        why it needs a test of its own rather than being caught by the
        bypass class."""
        self.assertEqual(self.search("q_m"), ["q_marks@renphil.org"])
        self.assertEqual(self.search("qXm"), [])

    def test_a_literal_backslash_in_a_name_is_findable(self) -> None:
        """The escape character itself. `A\\B` must reach `Sam A\\B` — if the
        backslash were passed through unescaped it would consume the `B` as
        an escaped literal and the pattern would mean something else."""
        self.assertEqual(self.search("A\\B"), ["sam@renphil.org"])

    def test_a_backslash_query_is_well_formed_and_matches_nothing_spurious(self) -> None:
        """Probes are three characters or longer because the router's
        `min_length=3` rejects anything shorter with a 422 before the service
        is reached. These must come back empty rather than raising a
        malformed-pattern error from the database."""
        for probe in ("\\\\\\", "a\\%b", "a\\_b", "x\\y"):
            with self.subTest(probe=probe):
                self.assertEqual(self.search(probe), [])


class TestSearchScopingIsUnchanged(SearchFixture):
    """§2.2 is the only thing being changed here. `has_signed_in` filtering is
    open finding §2.4 and is explicitly NOT in scope — this pins the
    behaviour so the escape work cannot have moved it by accident."""

    def test_inactive_people_are_still_excluded(self) -> None:
        self.db.query(UserV2).filter(UserV2.email == "bob@renphil.org").update(
            {"has_signed_in": False}
        )
        self.db.commit()
        self.assertEqual(self.search("bob"), [])

    def test_the_result_shape_is_unchanged(self) -> None:
        res = self.client.get("/v2/rbac/hub-users", params={"q": "ali"}, headers=self.admin)
        self.assertEqual(set(res.json()["data"][0].keys()), {"id", "email", "name"})

    def test_the_limit_still_applies_and_still_signals_nothing(self) -> None:
        """HUB_USER_SEARCH_LIMIT is 20 and there is no truncation signal —
        open finding §2.6, adjacent to this work and deliberately NOT fixed.
        Recorded here so whoever does fix it finds a test to update."""
        from app.services.rbac_assignment_service import HUB_USER_SEARCH_LIMIT

        now = datetime.now(timezone.utc)
        for i in range(HUB_USER_SEARCH_LIMIT + 5):
            self.db.add(
                UserV2(email=f"bulk{i:03d}@renphil.org", name=f"Bulk {i}", has_signed_in=True, created_at=now)
            )
        self.db.commit()

        res = self.client.get("/v2/rbac/hub-users", params={"q": "bulk"}, headers=self.admin)
        body = res.json()
        self.assertEqual(len(body["data"]), HUB_USER_SEARCH_LIMIT)
        self.assertEqual(set(body.keys()), {"data"})


# =========================================================================
# §2.3 — `/me/revocable` was quadratic in DB round-trips
#
# MEASURED, not asserted from a document. `list_revocable` asks
# `can_delegate` once per candidate row, and `can_delegate` used to rebuild
# the granter's own closures on every one of those calls: an `RbacClosures`
# (both edge tables, the public roles, the scopes) plus a `held_assignments`,
# five queries per row.
#
#   BEFORE, 71 assignment rows, non-admin with one held assignment: 357
#   AFTER : 7, flat in the number of rows
#   Hub Admin (bypasses the loop entirely), either way: 3
#
# The audit's own figure was 244. The shape had grown since — `RbacClosures`
# issues two more queries per instantiation than the free closure helpers it
# replaced — so the finding had got worse, not better, while it sat open.
#
# These tests assert an UPPER BOUND and a FLAT SHAPE rather than an exact
# number, so an unrelated extra query does not make them brittle. What they
# do pin exactly is that the ANSWER did not change.
# =========================================================================


class RevocableCostFixture(AdminFloorFixture):
    """Builds assignments straight through the ORM rather than the API: this
    needs tens of rows, and the point of measurement is the read path."""

    def setUp(self) -> None:
        super().setUp()
        now = datetime.now(timezone.utc)

        self.role_ids: dict[str, int] = {}
        for key, name in [
            ("hub_admin", "Hub Admin"),
            ("program_lead", "Program Lead"),
            ("program_member", "Program Member"),
            ("hub_member", "Hub Member"),
        ]:
            row = RoleV2(key=key, name=name, is_system=False, is_public=False, created_at=now)
            self.db.add(row)
            self.db.flush()
            self.role_ids[key] = row.id
        for parent, child in [
            ("hub_admin", "program_lead"),
            ("program_lead", "program_member"),
            ("program_member", "hub_member"),
        ]:
            self.db.execute(
                RoleEdgeV2.__table__.insert().values(
                    parent_role_id=self.role_ids[parent],
                    child_role_id=self.role_ids[child],
                    created_at=now,
                )
            )

        self.scope_ids: dict[str, int] = {}
        for key, name, universal in [
            ("all_scopes", "All Scopes", True),
            ("scope_a", "Scope A", False),
            ("scope_a_sub", "Scope A Sub", False),
        ]:
            row = ScopeV2(
                key=key,
                name=name,
                is_universal=universal,
                is_system=False,
                is_public=False,
                created_at=now,
            )
            self.db.add(row)
            self.db.flush()
            self.scope_ids[key] = row.id
        self.db.execute(
            ScopeEdgeV2.__table__.insert().values(
                parent_scope_id=self.scope_ids["scope_a"],
                child_scope_id=self.scope_ids["scope_a_sub"],
                created_at=now,
            )
        )

        # Alice holds exactly ONE assignment, matching the audit's scenario.
        alice = UserV2(email="alice@renphil.org", name="Alice", created_at=now)
        self.db.add(alice)
        self.db.flush()
        self.alice_id = alice.id
        self.db.add(
            RoleAssignmentV2(
                user_id=alice.id,
                role_id=self.role_ids["program_lead"],
                scope_id=self.scope_ids["scope_a"],
                created_at=now,
            )
        )
        self.db.commit()

    def add_people(self, n: int, role_key: str, scope_key: str, prefix: str) -> None:
        now = datetime.now(timezone.utc)
        for i in range(n):
            person = UserV2(
                email=f"{prefix}{i:03d}@renphil.org", name=f"{prefix} {i}", created_at=now
            )
            self.db.add(person)
            self.db.flush()
            self.db.add(
                RoleAssignmentV2(
                    user_id=person.id,
                    role_id=self.role_ids[role_key],
                    scope_id=self.scope_ids[scope_key],
                    created_at=now,
                )
            )
        self.db.commit()

    def count_statements(self, fn):
        """The audit's measurement method: a `before_cursor_execute` listener
        on the engine. Returns `(result, statement_count)`."""
        seen: list[str] = []

        def _record(_conn, _cursor, statement, _params, _context, _many):
            seen.append(statement)

        event.listen(self.engine, "before_cursor_execute", _record)
        try:
            result = fn()
        finally:
            event.remove(self.engine, "before_cursor_execute", _record)
        return result, len(seen)


class TestRevocableRoundTrips(RevocableCostFixture):
    # Generous enough that an unrelated extra query does not fail the suite,
    # tight enough that a return to per-row closure rebuilding cannot pass:
    # at 60 candidates the old shape cost 357.
    UPPER_BOUND = 20

    def test_a_non_admin_no_longer_pays_per_row(self) -> None:
        from app.services import rbac_assignment_service

        self.add_people(60, "program_member", "scope_a", "p")
        rows, statements = self.count_statements(
            lambda: rbac_assignment_service.list_revocable(
                self.db, self.alice_id, is_hub_admin=False
            )
        )
        self.assertEqual(len(rows), 60)
        self.assertLessEqual(statements, self.UPPER_BOUND, f"{statements} statements for 60 rows")

    def test_the_cost_is_FLAT_in_the_number_of_rows(self) -> None:
        """The bound above could be met by a merely-cheaper implementation
        that still scaled. This is the one that pins the SHAPE: ten rows and
        sixty rows must cost the same, because the caller's own closures are
        computed once either way."""
        from app.services import rbac_assignment_service

        def measure():
            return self.count_statements(
                lambda: rbac_assignment_service.list_revocable(
                    self.db, self.alice_id, is_hub_admin=False
                )
            )

        self.add_people(10, "program_member", "scope_a", "small")
        (rows_small, n_small) = measure()

        self.add_people(50, "program_member", "scope_a", "big")
        (rows_big, n_big) = measure()

        self.assertEqual(len(rows_small), 10)
        self.assertEqual(len(rows_big), 60)
        self.assertEqual(
            n_small,
            n_big,
            f"{n_small} statements for 10 rows vs {n_big} for 60 — still scaling",
        )

    def test_a_hub_admin_still_bypasses_the_loop_entirely(self) -> None:
        from app.services import rbac_assignment_service

        self.add_people(60, "program_member", "scope_a", "p")
        rows, statements = self.count_statements(
            lambda: rbac_assignment_service.list_revocable(
                self.db, self.alice_id, is_hub_admin=True
            )
        )
        self.assertEqual(len(rows), 61)  # everything, including Alice's own
        self.assertLessEqual(statements, 5)


class TestRevocableAnswerIsUnchanged(RevocableCostFixture):
    """A faster function that returns a different set is a regression, not a
    fix. The ORACLE here is `can_delegate` called the OLD way — without
    `held=` — which is still a supported call shape and still recomputes the
    closures per row. If the hoist changed any verdict, these diverge."""

    def setUp(self) -> None:
        super().setUp()
        # A deliberately mixed population so the answer is not trivially
        # "everything" or "nothing".
        self.add_people(12, "program_member", "scope_a", "yes")        # revocable
        self.add_people(6, "hub_member", "scope_a_sub", "deep")        # revocable (both closures)
        self.add_people(5, "program_lead", "scope_a", "peer")          # NOT — her own role
        self.add_people(4, "hub_admin", "scope_a", "senior")           # NOT — above her
        self.add_people(3, "program_member", "all_scopes", "wide")     # NOT — universal ∉ desc(A)
        self.add_people(2, "program_member", "scope_a_sub", "sub")     # revocable

    def unhoisted_answer(self) -> set[int]:
        """What the code did before §2.3: one `can_delegate` per row, each
        rebuilding the granter's closures from scratch."""
        from app.db_v2.models.role_assignment import RoleAssignmentV2 as RA
        from app.services.rbac_delegation_service import can_delegate

        return {
            row.id
            for row in self.db.query(RA).all()
            if can_delegate(self.db, self.alice_id, row.role_id, row.scope_id)
        }

    def test_the_same_rows_come_back(self) -> None:
        from app.services import rbac_assignment_service

        hoisted = {
            row["id"]
            for row in rbac_assignment_service.list_revocable(
                self.db, self.alice_id, is_hub_admin=False
            )
        }
        self.assertEqual(hoisted, self.unhoisted_answer())

    def test_the_answer_is_not_vacuous(self) -> None:
        """Without this the equality above could be `set() == set()`. 20 of
        the 33 rows are revocable and 13 are not, so both branches of the
        eligibility rule are exercised."""
        from app.db_v2.models.role_assignment import RoleAssignmentV2 as RA

        total = self.db.query(RA).count()
        revocable = len(self.unhoisted_answer())
        self.assertEqual(total, 33)
        self.assertEqual(revocable, 20)

    def test_the_ineligible_rows_are_the_expected_ones(self) -> None:
        """Named rather than counted, so a change of verdict says which."""
        from app.services import rbac_assignment_service

        emails = {
            row["user_email"]
            for row in rbac_assignment_service.list_revocable(
                self.db, self.alice_id, is_hub_admin=False
            )
        }
        for prefix in ("peer", "senior", "wide"):
            with self.subTest(prefix=prefix):
                self.assertFalse({e for e in emails if e.startswith(prefix)})
        for prefix in ("yes", "deep", "sub"):
            with self.subTest(prefix=prefix):
                self.assertTrue({e for e in emails if e.startswith(prefix)})


class TestCanDelegateHeldParameter(RevocableCostFixture):
    """`held=` is a data hoist, never a rule change. Quantified over the
    whole role × scope space rather than sampled."""

    def test_held_and_unheld_agree_on_every_pair(self) -> None:
        from app.services.rbac_delegation_service import can_delegate
        from app.services.rbac_graph_service import held_closures

        held = held_closures(self.db, self.alice_id)
        checked = 0
        for role_id in self.role_ids.values():
            for scope_id in self.scope_ids.values():
                with self.subTest(role=role_id, scope=scope_id):
                    self.assertEqual(
                        can_delegate(self.db, self.alice_id, role_id, scope_id),
                        can_delegate(self.db, self.alice_id, role_id, scope_id, held=held),
                    )
                checked += 1
        self.assertEqual(checked, 12)

    def test_the_space_contains_both_answers(self) -> None:
        """Guards the test above from passing because everything is False."""
        from app.services.rbac_delegation_service import can_delegate

        answers = {
            can_delegate(self.db, self.alice_id, r, s)
            for r in self.role_ids.values()
            for s in self.scope_ids.values()
        }
        self.assertEqual(answers, {True, False})

    def test_an_empty_held_list_refuses_everything(self) -> None:
        """`held=[]` is a caller with no assignments, and must not be
        confused with `held=None` (meaning "go and look them up"). A truthiness
        test instead of an `is None` test would silently re-query here and
        return True."""
        from app.services.rbac_delegation_service import can_delegate

        self.assertTrue(
            can_delegate(
                self.db, self.alice_id, self.role_ids["program_member"], self.scope_ids["scope_a"]
            )
        )
        self.assertFalse(
            can_delegate(
                self.db,
                self.alice_id,
                self.role_ids["program_member"],
                self.scope_ids["scope_a"],
                held=[],
            )
        )


# =========================================================================
# The last-admin guard
# (plan_access_control_algorithm_2026-08-27.md §6.5, §6.8)
# =========================================================================


class AdminGuardFixture(AdminFloorFixture):
    def setUp(self) -> None:
        super().setUp()
        self.build_org()
        self.people = [f"admin{i}@renphil.org" for i in range(3)]
        self.people.append("ordinary@renphil.org")
        for email in self.people:
            self.provision(self.user(email))

    def make_admin(self, email: str) -> dict:
        return self.grant(self.admin, email, self.hub_admin_role, self.all_scopes)

    def revoke(self, assignment_id: int, headers: dict | None = None):
        return self.client.delete(
            f"/v2/rbac/assignments/{assignment_id}", headers=headers or self.admin
        )

    def admin_assignment_count(self) -> int:
        return (
            self.db.query(RoleAssignmentV2)
            .filter(
                RoleAssignmentV2.role_id == self.hub_admin_role,
                RoleAssignmentV2.scope_id == self.all_scopes,
            )
            .count()
        )


class TestTheLastAdminCannotBeRevoked(AdminGuardFixture):
    """Since phase 3 the caller is a real admin, so the only hub-wide admin
    row is the caller's own (`self.admin_assignment_id`)."""

    def test_the_only_hub_wide_admin_assignment_is_refused(self) -> None:
        res = self.revoke(self.admin_assignment_id)
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(self.code_of(res), "last_hub_admin")

    def test_a_HUB_ADMIN_cannot_revoke_them_either(self) -> None:
        """THE WHOLE POINT, and the one place in this codebase where the Hub
        Admin bypass must not come first.

        Every other gate here lets a Hub Admin through unconditionally. Only
        a Hub Admin can revoke a Hub Admin's assignment in the first place —
        §6.1 needs a role strictly above `hub_admin` and nothing is above it
        — so a guard the bypass skipped would never run for the only person
        who can trip it.

        `self.admin` holds a real `(hub_admin, all_scopes)` row, so every
        revoke in this class is already made as one. This test exists to say
        so, at the assertion, rather than leaving it implicit in a fixture."""
        only = self.admin_assignment_id

        # The bypass demonstrably works for the AUTHORIZATION gate...
        peer = self.grant(self.admin, self.people[3], self.program_lead, self.scope_a)
        self.assertEqual(self.revoke(peer["id"]).status_code, 204)

        # ...and demonstrably does not apply to the integrity rule.
        res = self.revoke(only)
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(self.code_of(res), "last_hub_admin")

    def test_the_refused_revoke_leaves_the_row_in_place(self) -> None:
        only = self.admin_assignment_id
        self.revoke(only)
        self.db.expire_all()
        self.assertEqual(self.admin_assignment_count(), 1)
        self.assertIsNotNone(
            self.db.query(RoleAssignmentV2).filter(RoleAssignmentV2.id == only).first()
        )

    def test_the_error_names_the_assignment_and_the_pair(self) -> None:
        only = {"id": self.admin_assignment_id}
        detail = self.revoke(only["id"]).json()["detail"]
        self.assertEqual(detail["assignment_id"], only["id"])
        self.assertEqual(detail["role_id"], self.hub_admin_role)
        self.assertEqual(detail["scope_id"], self.all_scopes)


class TestTheGuardIsNotRefusingEverything(AdminGuardFixture):
    """The caller's seeded admin row is always one of the admins counted."""

    def test_the_second_to_last_CAN_be_revoked(self) -> None:
        first = self.make_admin(self.people[0])
        self.assertEqual(self.admin_assignment_count(), 2)

        self.assertEqual(self.revoke(first["id"]).status_code, 204)
        self.db.expire_all()
        self.assertEqual(self.admin_assignment_count(), 1)

    def test_revoking_down_to_one_then_stopping(self) -> None:
        """Three admins, revoked one at a time. The first two succeed and the
        third (the caller's own row) is refused — the floor is a floor, not a
        freeze."""
        ids = [self.make_admin(email)["id"] for email in self.people[:2]]
        ids.append(self.admin_assignment_id)
        self.assertEqual(self.admin_assignment_count(), 3)
        self.assertEqual(self.revoke(ids[0]).status_code, 204)
        self.assertEqual(self.revoke(ids[1]).status_code, 204)
        res = self.revoke(ids[2])
        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.code_of(res), "last_hub_admin")
        self.db.expire_all()
        self.assertEqual(self.admin_assignment_count(), 1)

    def test_an_ordinary_assignment_is_unaffected_while_one_admin_exists(self) -> None:
        self.assertEqual(self.admin_assignment_count(), 1)
        ordinary = self.grant(
            self.admin, self.people[3], self.program_member, self.scope_a
        )
        self.assertEqual(self.revoke(ordinary["id"]).status_code, 204)


class TestScopeMattersToTheGuard(AdminGuardFixture):
    """§6.5 condition 1: a Hub Admin must be assigned on the UNIVERSAL scope.
    `(Hub Admin, Scope A)` is a program-limited admin and is NOT the floor —
    confirmed with the owner 2026-08-31 as the literal-pair reading of §6.8,
    not a closure match."""

    def test_hub_admin_on_a_NON_universal_scope_is_not_protected(self) -> None:
        scoped = self.grant(self.admin, self.people[0], self.hub_admin_role, self.scope_a)
        self.assertEqual(self.admin_assignment_count(), 1)  # the caller's own only
        self.assertEqual(self.revoke(scoped["id"]).status_code, 204)

    def test_a_scoped_admin_does_not_satisfy_the_floor_for_a_universal_one(self) -> None:
        """The dangerous direction. A `(Hub Admin, Scope A)` row must not
        count towards the floor and let the real hub-wide admin (the
        caller's own row) be revoked."""
        universal = {"id": self.admin_assignment_id}
        self.grant(self.admin, self.people[1], self.hub_admin_role, self.scope_a)

        res = self.revoke(universal["id"])
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(self.code_of(res), "last_hub_admin")

    def test_a_universal_assignment_on_a_DIFFERENT_role_is_not_protected(self) -> None:
        """`(Program Lead, All Scopes)` is hub-wide but is not an admin."""
        wide = self.grant(self.admin, self.people[0], self.program_lead, self.all_scopes)
        self.assertEqual(self.revoke(wide["id"]).status_code, 204)

    def test_the_role_is_matched_by_KEY_not_by_display_name(self) -> None:
        """`HUB_ADMIN_ROLE` in tab_service is the JWT display name "Hub
        Admin"; the guard must key on `roles.key = 'hub_admin'`. Renaming the
        role must not disarm the floor — that is the whole reason key and
        name are separate columns (RoleV2's comment)."""
        only = {"id": self.admin_assignment_id}
        res = self.client.patch(
            f"/v2/rbac/roles/{self.hub_admin_role}",
            json={"name": "Chief Administrator"},
            headers=self.admin,
        )
        self.assertEqual(res.status_code, 200, res.text)

        refused = self.revoke(only["id"])
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertEqual(self.code_of(refused), "last_hub_admin")


class TestTheBootstrapIsNotBroken(AdminGuardFixture):
    """HAZARD 3. `role_assignments` is empty in production right now, so a
    guard reasoning about "the count would drop to zero" has to behave
    correctly when the count is ALREADY zero. Deleting an unrelated
    assignment in a hub with no admins at all must not error.

    Since phase 3 the org can only be built by a real admin, so setUp builds
    it, has the admin grant a delegator and an ordinary assignment, then
    drops the admin's own row. With zero admins nobody is a Hub Admin, so
    the revoke is made by the delegator (Program Lead on A)."""

    def setUp(self) -> None:
        super().setUp()
        self.delegator = self.user(self.people[0])
        self.grant(self.admin, self.people[0], self.program_lead, self.scope_a)
        self.ordinary = self.grant(self.admin, self.people[3], self.program_member, self.scope_a)
        self._drop_admin_assignment()

    def test_the_tables_really_are_empty_at_the_start(self) -> None:
        """Without this the rest of the class could pass vacuously against a
        fixture that had quietly acquired an admin. Same guard step 5's
        `TestTheBootstrapIsNotDeadlocked` uses."""
        self.assertEqual(self.admin_assignment_count(), 0)

    def test_revoking_an_ordinary_assignment_with_ZERO_admins_succeeds(self) -> None:
        ordinary = self.ordinary
        self.assertEqual(self.admin_assignment_count(), 0)
        res = self.revoke(ordinary["id"], headers=self.delegator)
        self.assertEqual(res.status_code, 204, res.text)

    def test_granting_the_FIRST_admin_still_works(self) -> None:
        """The guard is on delete only. It must not have made the bootstrap
        harder in the other direction."""
        self.assertEqual(self.admin_assignment_count(), 0)
        self.make_admin(self.people[0])
        self.assertEqual(self.admin_assignment_count(), 1)

    def test_revoking_a_nonexistent_assignment_is_still_404(self) -> None:
        self.assertEqual(self.revoke(999_999).status_code, 404)


class TestTheGuardWithoutTheRowsItLooksFor(AdminFloorFixture):
    """The guard resolves `hub_admin` and the universal scope by lookup. A
    database missing either must not break ordinary revocation — it means the
    protected pair cannot exist, so there is nothing to protect.

    Since phase 3 only a real admin can build the rows, and that admin needs
    exactly the `hub_admin` role and universal scope these tests remove. So
    each test builds through the seeded admin, then deletes the admin's row
    and the role (or scope) straight from the database. With no admin left,
    the revoke is made by a delegator whose role sits above the target's."""

    def setUp(self) -> None:
        super().setUp()
        self.target = "target@renphil.org"
        self.provision(self.user(self.target))
        self.delegator_email = "delegator@renphil.org"
        self.delegator = self.user(self.delegator_email)
        self.provision(self.delegator)
        self.scope_a = self.make_scope("scope_a", "Scope A")

    def test_no_hub_admin_ROLE_row_at_all(self) -> None:
        lead = self.make_role("program_lead", "Program Lead")
        member = self.make_role("program_member", "Program Member")
        self.add_role_edge(lead, member)
        self.grant(self.admin, self.delegator_email, lead, self.scope_a)
        assignment = self.grant(self.admin, self.target, member, self.scope_a)

        self._drop_admin_assignment()
        self.db.query(RoleV2).filter(RoleV2.id == self.hub_admin_role).delete()
        self.db.commit()
        self.assertEqual(self.db.query(RoleV2).filter(RoleV2.key == "hub_admin").count(), 0)

        res = self.client.delete(
            f"/v2/rbac/assignments/{assignment['id']}", headers=self.delegator
        )
        self.assertEqual(res.status_code, 204, res.text)

    def test_no_UNIVERSAL_scope_row_at_all(self) -> None:
        above = self.make_role("above_hub_admin", "Above Hub Admin")
        self.add_role_edge(above, self.hub_admin_role)
        self.grant(self.admin, self.delegator_email, above, self.scope_a)
        assignment = self.grant(self.admin, self.target, self.hub_admin_role, self.scope_a)

        self._drop_admin_assignment()
        self.db.query(ScopeV2).filter(ScopeV2.id == self.all_scopes).delete()
        self.db.commit()
        self.assertEqual(self.db.query(ScopeV2).filter(ScopeV2.is_universal.is_(True)).count(), 0)

        res = self.client.delete(
            f"/v2/rbac/assignments/{assignment['id']}", headers=self.delegator
        )
        self.assertEqual(res.status_code, 204, res.text)


class TestTheNominalFloorWarning(AdminGuardFixture):
    """Owner decision, 2026-08-31: the count ignores `has_signed_in` (open finding
    §2.4 — the flag is enforced nowhere else, and making the admin floor the
    one exception would be inconsistent), but an allowed revoke that leaves no
    ACTIVE admin says so."""

    def deactivate(self, email: str) -> None:
        self.db.query(UserV2).filter(UserV2.email == email).update({"has_signed_in": False})
        self.db.commit()

    # "first" below is the caller's own seeded admin row: since phase 3 the
    # caller is a real admin, so leaving ONLY a deactivated admin behind
    # means the caller revokes their own row.

    def test_it_warns_when_the_only_remaining_admin_is_deactivated(self) -> None:
        first = {"id": self.admin_assignment_id}
        self.make_admin(self.people[1])
        self.deactivate(self.people[1])

        with self.assertLogs("app.services.rbac_assignment_service", level="WARNING") as cm:
            self.assertEqual(self.revoke(first["id"]).status_code, 204)
        self.assertIn("Admin floor is nominal", "\n".join(cm.output))

    def test_it_does_NOT_warn_when_an_active_admin_remains(self) -> None:
        first = self.make_admin(self.people[0])
        self.make_admin(self.people[1])

        with self.assertNoLogs("app.services.rbac_assignment_service", level="WARNING"):
            self.assertEqual(self.revoke(first["id"]).status_code, 204)

    def test_a_deactivated_admin_STILL_satisfies_the_floor(self) -> None:
        """The other half of the same decision, and the cost of it. The
        deactivated person cannot log in, yet the last hub-wide admin
        assignment is still refused because they hold one. Changing this is a
        decision about §2.4, not a bug fix here.

        The final revoke goes through the service: once the caller has
        revoked their own row nobody signed in is an admin, so no router
        call could reach the guard (TestTheGuardLivesInTheService shows the
        service is where it runs)."""
        from app.services import rbac_assignment_service
        from app.services.rbac_graph_service import RbacGraphError

        first = {"id": self.admin_assignment_id}
        self.make_admin(self.people[1])
        self.deactivate(self.people[1])
        self.assertEqual(self.revoke(first["id"]).status_code, 204)

        self.db.expire_all()
        remaining = (
            self.db.query(RoleAssignmentV2)
            .filter(
                RoleAssignmentV2.role_id == self.hub_admin_role,
                RoleAssignmentV2.scope_id == self.all_scopes,
            )
            .one()
        )
        with self.assertRaises(RbacGraphError) as ctx:
            rbac_assignment_service.delete_assignment(self.db, remaining.id)
        self.assertEqual(ctx.exception.code, "last_hub_admin")


class TestTheGuardLivesInTheService(AdminGuardFixture):
    """It is an INTEGRITY rule, not an authorization one, so it sits beside
    the write rather than at the router — the same place `delete_role`'s
    `role_in_use` pre-count lives. Driving the service directly proves the
    protection is not something a future caller could route around by
    skipping the router."""

    def test_the_service_refuses_directly_with_no_router_involved(self) -> None:
        from app.services import rbac_assignment_service
        from app.services.rbac_graph_service import RbacGraphError

        only = {"id": self.admin_assignment_id}  # the caller's own, and the only one
        self.db.expire_all()

        with self.assertRaises(RbacGraphError) as ctx:
            rbac_assignment_service.delete_assignment(self.db, only["id"])
        self.assertEqual(ctx.exception.code, "last_hub_admin")

    def test_the_service_takes_no_is_hub_admin_argument(self) -> None:
        """Pins the absence. Adding one would be the "correction" this
        guard's docstring exists to pre-empt: a bypass parameter here makes
        the rule decorative, since only a Hub Admin can reach it."""
        import inspect

        from app.services import rbac_assignment_service

        for fn in (
            rbac_assignment_service.delete_assignment,
            rbac_assignment_service._assert_not_the_last_hub_admin,
        ):
            with self.subTest(fn=fn.__name__):
                params = set(inspect.signature(fn).parameters)
                self.assertNotIn("is_hub_admin", params)


# =========================================================================
# §2.1 — `is_system` is set by script, and cannot be cleared
# =========================================================================


class TestIsSystemCannotBeCleared(AdminFloorFixture):
    """HAZARD 5. `update_role` writes only name/description/depth and
    `update_scope` only name/description, so a set flag cannot be cleared
    through the API. That was an ACCIDENT of which fields the updater happens
    to write; these tests make it a stated rule, because it is the thing that
    makes the delete-guard durable.

    (`delete_role`/`delete_scope` refusing a system row is already covered by
    `test_rbac_api.test_system_role_cannot_be_deleted` and
    `test_system_scope_cannot_be_deleted` — checked before adding, not
    duplicated here.)"""

    def setUp(self) -> None:
        super().setUp()
        # The seeded admin's own role and scope (`_ensure_admin`).
        self._ensure_admin()
        self.role_id = self.hub_admin_role
        self.scope_id = self.all_scopes
        # What the script does, applied here directly.
        self.db.query(RoleV2).filter(RoleV2.id == self.role_id).update({"is_system": True})
        self.db.query(ScopeV2).filter(ScopeV2.id == self.scope_id).update({"is_system": True})
        self.db.commit()

    def role_row(self) -> RoleV2:
        self.db.expire_all()
        return self.db.query(RoleV2).filter(RoleV2.id == self.role_id).one()

    def scope_row(self) -> ScopeV2:
        self.db.expire_all()
        return self.db.query(ScopeV2).filter(ScopeV2.id == self.scope_id).one()

    def test_sending_is_system_false_to_a_role_is_422_not_a_clear(self) -> None:
        res = self.client.patch(
            f"/v2/rbac/roles/{self.role_id}", json={"is_system": False}, headers=self.admin
        )
        self.assertEqual(res.status_code, 422, res.text)
        self.assertTrue(self.role_row().is_system)

    def test_sending_is_system_false_to_a_scope_is_422_not_a_clear(self) -> None:
        res = self.client.patch(
            f"/v2/rbac/scopes/{self.scope_id}", json={"is_system": False}, headers=self.admin
        )
        self.assertEqual(res.status_code, 422, res.text)
        self.assertTrue(self.scope_row().is_system)

    def test_an_ordinary_role_update_leaves_the_flag_alone(self) -> None:
        """The realistic path — nobody sends `is_system`, they rename the
        role. The flag must survive that, and `depth` being cleared in the
        same request must not disturb it either."""
        res = self.client.patch(
            f"/v2/rbac/roles/{self.role_id}",
            json={"name": "Chief Administrator", "description": "x", "depth": None},
            headers=self.admin,
        )
        self.assertEqual(res.status_code, 200, res.text)
        row = self.role_row()
        self.assertTrue(row.is_system)
        self.assertEqual(row.name, "Chief Administrator")
        self.assertEqual(row.key, "hub_admin")

    def test_an_ordinary_scope_update_leaves_the_flag_alone(self) -> None:
        res = self.client.patch(
            f"/v2/rbac/scopes/{self.scope_id}",
            json={"name": "Everything", "description": "x"},
            headers=self.admin,
        )
        self.assertEqual(res.status_code, 200, res.text)
        self.assertTrue(self.scope_row().is_system)

    def test_the_flag_is_reported_on_the_wire(self) -> None:
        """It has to be visible, or the UI cannot explain why delete is
        refused."""
        res = self.client.get(f"/v2/rbac/roles/{self.role_id}", headers=self.admin)
        self.assertTrue(res.json()["data"]["is_system"])


class TestIsSystemStaysScriptOnly(AdminFloorFixture):
    """Owner decision, 2026-08-31: script-only, NOT settable through the API.
    Because `update_role` cannot clear it, an admin who set it by accident on
    create would have made a permanently undeletable row."""

    def test_is_system_is_refused_on_role_create(self) -> None:
        res = self.client.post(
            "/v2/rbac/roles",
            json={"key": "x_role", "name": "X Role", "is_system": True},
            headers=self.admin,
        )
        self.assertEqual(res.status_code, 422, res.text)

    def test_is_system_is_refused_on_scope_create(self) -> None:
        res = self.client.post(
            "/v2/rbac/scopes",
            json={"key": "x_scope", "name": "X Scope", "is_universal": False, "is_system": True},
            headers=self.admin,
        )
        self.assertEqual(res.status_code, 422, res.text)

    def test_an_ordinary_create_lands_false(self) -> None:
        role_id = self.make_role("y_role", "Y Role")
        self.assertFalse(self.db.query(RoleV2).filter(RoleV2.id == role_id).one().is_system)


class TestTheIsSystemScriptPlan(AdminFloorFixture):
    """`scripts/set_rbac_system_flags.py` is Postgres-only and is never run by
    the suite, but its DECISION function is plain SQLAlchemy and is what the
    idempotence and refusal claims rest on. Tested directly against the
    in-memory schema.

    Rows are written through the ORM, not the API: since phase 3 the API
    needs a real admin, i.e. a `hub_admin` role and a universal scope, and
    several tests here are about those rows being absent."""

    def make_role(self, key: str, name: str, public: bool = False) -> int:
        row = RoleV2(key=key, name=name, is_public=public, is_system=False, created_at=datetime.now(timezone.utc))
        self.db.add(row)
        self.db.commit()
        return row.id

    def make_scope(self, key: str, name: str, universal: bool = False, public: bool = False) -> int:
        row = ScopeV2(
            key=key,
            name=name,
            is_universal=universal,
            is_public=public,
            is_system=False,
            created_at=datetime.now(timezone.utc),
        )
        self.db.add(row)
        self.db.commit()
        return row.id

    def plan(self):
        from scripts.set_rbac_system_flags import _plan

        return _plan(self.db)

    def test_it_marks_hub_admin_and_the_universal_scope(self) -> None:
        role_id = self.make_role("hub_admin", "Hub Admin")
        scope_id = self.make_scope("all_scopes", "All Scopes", universal=True)

        to_update, _lines, problems = self.plan()
        self.assertEqual(problems, [])
        self.assertEqual(
            {(kind, row.id) for kind, row in to_update},
            {("role", role_id), ("scope", scope_id)},
        )

    def test_it_is_idempotent(self) -> None:
        """A second run reports nothing to do. Applied here by doing what
        `--commit` does — setting the attribute — rather than by re-running
        the script against a database it refuses to touch."""
        self.make_role("hub_admin", "Hub Admin")
        self.make_scope("all_scopes", "All Scopes", universal=True)

        to_update, _lines, _problems = self.plan()
        self.assertEqual(len(to_update), 2)
        for _kind, row in to_update:
            row.is_system = True
        self.db.commit()

        to_update, lines, problems = self.plan()
        self.assertEqual(to_update, [])
        self.assertEqual(problems, [])
        self.assertTrue(all("already is_system" in line for line in lines))

    def test_it_does_NOT_mark_the_bottom_rows_or_hub_member(self) -> None:
        """Owner decision, 2026-08-31. ⊥ exists in live Neon (`any_role` id
        31, `any_scope` id 6) and is deliberately left unmarked; so is
        `hub_member`, whose §11.1 role is unsettled."""
        self.make_role("hub_admin", "Hub Admin")
        self.make_scope("all_scopes", "All Scopes", universal=True)
        any_role = self.make_role("any_role", "Any Role", public=True)
        any_scope = self.make_scope("any_scope", "Any Scope", public=True)
        hub_member = self.make_role("hub_member", "Hub Member")
        content_scope = self.make_scope("ai_for_math", "AI for Math")

        to_update, _lines, _problems = self.plan()
        touched = {row.id for _kind, row in to_update}
        for unmarked in (any_role, any_scope, hub_member, content_scope):
            with self.subTest(id=unmarked):
                self.assertNotIn(unmarked, touched)

    def test_it_targets_hub_admin_BY_KEY_not_whichever_role_is_first(self) -> None:
        """ADDED AFTER A MUTATION SURVIVED. Every other test in this class
        creates `hub_admin` first, so a plan that simply took the
        lowest-id role would have picked it anyway and looked correct.

        Here two ordinary roles are created BEFORE it, so `hub_admin` has
        neither the lowest id nor the alphabetically first name. Marking the
        wrong row is the one mistake this script cannot undo — `is_system`
        has no API path that clears it — so "it named its target explicitly"
        needs to be a test rather than a claim in a docstring."""
        contractor = self.make_role("contractor", "Contractor")
        program_lead = self.make_role("program_lead", "Program Lead")
        hub_admin = self.make_role("hub_admin", "Hub Admin")
        self.make_scope("all_scopes", "All Scopes", universal=True)

        self.assertLess(contractor, hub_admin, "fixture must not put hub_admin first")

        to_update, _lines, problems = self.plan()
        self.assertEqual(problems, [])
        marked_roles = {row.id for kind, row in to_update if kind == "role"}
        self.assertEqual(marked_roles, {hub_admin})
        self.assertNotIn(contractor, marked_roles)
        self.assertNotIn(program_lead, marked_roles)

    def test_it_refuses_when_roles_EXIST_but_none_is_hub_admin(self) -> None:
        """The other half of the same gap. A database with roles but no
        `hub_admin` must REFUSE, not fall back to marking something else —
        that is the "is this even the right database?" check."""
        self.make_role("contractor", "Contractor")
        self.make_role("program_lead", "Program Lead")
        self.make_scope("all_scopes", "All Scopes", universal=True)

        to_update, _lines, problems = self.plan()
        self.assertTrue(any("hub_admin" in p for p in problems))
        self.assertEqual([kind for kind, _row in to_update], ["scope"])

    def test_it_refuses_without_a_hub_admin_role(self) -> None:
        self.make_scope("all_scopes", "All Scopes", universal=True)
        _to_update, _lines, problems = self.plan()
        self.assertTrue(any("hub_admin" in p for p in problems))

    def test_it_refuses_without_a_universal_scope(self) -> None:
        self.make_role("hub_admin", "Hub Admin")
        self.make_scope("scope_a", "Scope A")
        _to_update, _lines, problems = self.plan()
        self.assertTrue(any("is_universal" in p for p in problems))

    def test_a_refusal_reports_nothing_to_update(self) -> None:
        """Belt and braces: the caller checks `problems` before `to_update`,
        but a plan that both refused AND proposed writes would be a trap."""
        self.make_scope("all_scopes", "All Scopes", universal=True)
        to_update, _lines, problems = self.plan()
        self.assertTrue(problems)
        self.assertEqual([kind for kind, _row in to_update], ["scope"])


# =========================================================================
# UPDATED for project_ac_enforcement_gap.md item 1 (2026-09-03): content
# endpoints now consult the folds (test_tab_visibility_wiring.py owns the
# exhaustive coverage). This class's own two assertions below happened to
# stay literally true — both endpoints are still 200, just with a FILTERED,
# now-empty list rather than an unfiltered one — so it is tightened to
# assert that emptiness explicitly rather than pass for the wrong reason.
# =========================================================================


class TestContentVisibilityAndTheAdminGate(AdminGuardFixture):
    def test_a_user_holding_nothing_sees_an_empty_hub_not_a_403(self) -> None:
        nobody = self.user("nobody@renphil.org")
        for path in ("/v2/nav-tabs", "/v2/tabs/root"):
            with self.subTest(path=path):
                res = self.client.get(path, headers=nobody)
                self.assertEqual(res.status_code, 200, res.text)
                self.assertEqual(res.json()["data"], [], "D2: unset access control matches no one")

    def test_require_hub_admin_was_not_touched(self) -> None:
        """§6.8 / §10 item 6 belong with the cutover and its ordering is "not
        negotiable". The role/scope definition writes must still be gated on
        the JWT identity check, unchanged."""
        res = self.client.post(
            "/v2/rbac/roles",
            json={"key": "z_role", "name": "Z Role"},
            headers=self.user("nobody@renphil.org"),
        )
        self.assertEqual(res.status_code, 403, res.text)
