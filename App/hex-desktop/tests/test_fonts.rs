//! Brand-font embedding proof (parity plan P0.1).
//!
//! The mockups are set in Apfel Grotezk (400/700) and Geist Mono (400/500).
//! GPUI's Windows text system (DirectWrite) only accepts TTF/OTF, so the
//! woff2 sources are pre-converted to static TTFs and embedded in
//! `fonts.rs`, then registered in `init_app`.
//!
//! gpui test contexts run on `NoopTextSystem`, which resolves every family to
//! a stub id, so runtime resolution cannot be asserted here (it is covered by
//! the visual verification loop). What CAN be proven is the contract the
//! resolver depends on: each embedded file is a well-formed sfnt whose family
//! name and OS/2 weight class match what the theme requests.

use gpui::TestAppContext;

fn be16(data: &[u8], o: usize) -> u16 {
    u16::from_be_bytes([data[o], data[o + 1]])
}

fn be32(data: &[u8], o: usize) -> u32 {
    u32::from_be_bytes([data[o], data[o + 1], data[o + 2], data[o + 3]])
}

/// Parses an sfnt (TTF/OTF) and returns its preferred family name (nameID 16,
/// falling back to nameID 1) and OS/2 usWeightClass.
fn parse_sfnt(data: &[u8]) -> (String, u16) {
    let version = be32(data, 0);
    assert!(
        version == 0x0001_0000 || version == 0x4F54_544F,
        "not a valid sfnt (version {version:#x}); DirectWrite would reject it"
    );

    let num_tables = be16(data, 4) as usize;
    let mut name_offset = None;
    let mut os2_offset = None;
    for i in 0..num_tables {
        let rec = 12 + i * 16;
        match &data[rec..rec + 4] {
            b"name" => name_offset = Some(be32(data, rec + 8) as usize),
            b"OS/2" => os2_offset = Some(be32(data, rec + 8) as usize),
            _ => {}
        }
    }

    let name = name_offset.expect("sfnt has no 'name' table");
    let count = be16(data, name + 2) as usize;
    let strings = name + be16(data, name + 4) as usize;

    let decode = |platform: u16, bytes: &[u8]| -> String {
        if platform == 3 {
            // Windows: UTF-16BE
            String::from_utf16_lossy(
                &bytes
                    .chunks_exact(2)
                    .map(|c| u16::from_be_bytes([c[0], c[1]]))
                    .collect::<Vec<_>>(),
            )
        } else {
            // Macintosh: ASCII-ish
            String::from_utf8_lossy(bytes).into_owned()
        }
    };

    let mut preferred = None;
    let mut fallback = None;
    for i in 0..count {
        let rec = name + 6 + i * 12;
        let platform = be16(data, rec);
        let name_id = be16(data, rec + 6);
        let len = be16(data, rec + 8) as usize;
        let off = be16(data, rec + 10) as usize;
        if name_id == 16 || name_id == 1 {
            let text = decode(platform, &data[strings + off..strings + off + len]);
            if name_id == 16 && preferred.is_none() {
                preferred = Some(text);
            } else if name_id == 1 && fallback.is_none() {
                fallback = Some(text);
            }
        }
    }

    let family = preferred.or(fallback).expect("sfnt name table lacks a family name");

    let os2 = os2_offset.expect("sfnt has no 'OS/2' table");
    let weight = be16(data, os2 + 4);
    (family, weight)
}

#[gpui::test]
async fn test_embedded_brand_fonts_match_mockup_families(cx: &mut TestAppContext) {
    // Exercising init_app keeps the runtime registration path covered (it must
    // not panic or log-error even under the Noop text system).
    cx.update(|cx| hex_desktop::chat::init_app(cx));

    for (file, family, weight) in [
        ("ApfelGrotezk-Regular.ttf", "Apfel Grotezk", 400),
        ("ApfelGrotezk-Bold.ttf", "Apfel Grotezk", 700),
        ("GeistMono-Regular.ttf", "Geist Mono", 400),
        ("GeistMono-Medium.ttf", "Geist Mono", 500),
    ] {
        let bytes = hex_desktop::fonts::embedded_font(file)
            .unwrap_or_else(|| panic!("missing embedded font {file}"));
        let (got_family, got_weight) = parse_sfnt(bytes);
        assert_eq!(got_family, family, "{file} family name mismatch");
        assert_eq!(got_weight, weight, "{file} OS/2 weight class mismatch");
    }
}
