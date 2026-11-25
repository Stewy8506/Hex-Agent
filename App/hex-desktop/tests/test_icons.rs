//! P0.3 acceptance: every icon asset the app references exists and parses as
//! a mockup-convention stroke SVG. The palette's chat/trash entries previously
//! pointed at missing assets (invisible icons); this pins the full manifest.

use std::fs;
use std::path::PathBuf;

fn icons_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("assets/icons")
}

fn parse_svg(name: &str) -> Result<(), String> {
    let path = icons_dir().join(format!("{name}.svg"));
    let text = fs::read_to_string(&path)
        .map_err(|e| format!("{name}.svg unreadable: {e}"))?;
    let t = text.trim();
    let header = r#"<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round">"#;
    if !t.starts_with(header) {
        return Err(format!("{name}.svg header deviates from the mockup convention"));
    }
    if !t.ends_with("</svg>") {
        return Err(format!("{name}.svg is truncated (no closing </svg>)"));
    }
    let body_len = t.len() - header.len() - "</svg>".len();
    if body_len == 0 {
        return Err(format!("{name}.svg has no shape elements"));
    }
    Ok(())
}

#[test]
fn test_missing_mockup_icons_now_exist() {
    // The 15 assets added from the parity plan §2.5 manifest.
    let added = [
        "folder",
        "chevron-right",
        "hamburger",
        "chat",
        "files",
        "browser",
        "terminal",
        "device",
        "more",
        "thumbs-up",
        "thumbs-down",
        "retry",
        "trash",
        "check",
        "x",
    ];
    for name in added {
        let path = icons_dir().join(format!("{name}.svg"));
        assert!(path.is_file(), "missing icon asset: {name}.svg");
    }
}

#[test]
fn test_palette_icon_references_resolve() {
    // palette.rs action rows reference these names via icon_svg(); both were
    // invisible before P0.3 added the assets.
    for name in ["chat", "trash"] {
        assert!(
            icons_dir().join(format!("{name}.svg")).is_file(),
            "palette references missing icon: {name}.svg"
        );
    }
}

#[test]
fn test_settings_icon_is_a_gear() {
    let settings = fs::read_to_string(icons_dir().join("settings.svg")).unwrap();
    assert!(settings.contains(r#"<circle cx="12" cy="12" r="3"/>"#));
    assert!(settings.contains(r#"d="M19.4 15"#));
}

#[test]
fn test_every_icon_asset_parses_as_mockup_svg() {
    let mut count = 0;
    for entry in fs::read_dir(icons_dir()).expect("icons dir readable") {
        let entry = entry.expect("icon dir entry readable");
        let name = entry.path().file_stem().unwrap().to_string_lossy().to_string();
        if let Err(err) = parse_svg(&name) {
            panic!("{err}");
        }
        count += 1;
    }
    assert!(
        count >= 30,
        "expected the full 30-icon manifest (15 existing + 15 added), found {count}"
    );
}
