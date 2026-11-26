"""The built frontend is served by the same process; client-side routes fall back to index.html."""


def test_serves_the_built_frontend_with_spa_fallback(start_app, tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<div id=root></div>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    client = start_app(static_dir=dist)

    assert client.get("/").text == "<div id=root></div>"
    assert client.get("/runs/12").text == "<div id=root></div>"
    assert client.get("/assets/app.js").text == "console.log(1)"
    assert client.get("/../../etc/passwd").text == "<div id=root></div>"
    assert client.get("/api/nope").status_code == 404
    assert client.get("/api/runs").status_code == 200
