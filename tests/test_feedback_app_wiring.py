"""Import the actual application to catch incomplete router/service changes."""
def test_actual_app_registers_both_feedback_paths_once():
    from app.main import app
    from app.routers import airtable, hub_feedback
    from app.dependencies import get_current_hub_user, get_airtable_service

    # Current FastAPI includes routers lazily; app.routes need not be a flat
    # list of APIRoute instances. OpenAPI proves actual factory inclusion.
    paths = app.openapi()["paths"]
    assert "post" in paths["/data/feedbacks"]
    assert {"get", "post"}.issubset(paths["/v2/hub-feedback"])

    def one(source, path, method):
        routes = [route for route in source.routes if getattr(route, "path", None) == path and method in getattr(route, "methods", set())]
        assert len(routes) == 1
        return routes[0]

    legacy = one(airtable.router, "/data/feedbacks", "POST")
    assert legacy.endpoint is airtable.create_feedback
    dependencies = {entry.call for entry in legacy.dependant.dependencies}
    assert get_current_hub_user in dependencies
    assert get_airtable_service not in dependencies
    assert one(hub_feedback.router, "/v2/hub-feedback", "POST").endpoint is hub_feedback.submit
    assert one(hub_feedback.router, "/v2/hub-feedback", "GET").endpoint is hub_feedback.list_entries
