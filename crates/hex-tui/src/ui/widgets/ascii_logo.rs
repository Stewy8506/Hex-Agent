//! ASCII and Unicode block-character wordmarks exactly matching the HEX Agent mockup.

use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::Style;
use ratatui::widgets::Widget;

/// Full 4-row chunky geometric pixel-block HEX Agent wordmark for the Start / Welcome screen.
///
/// Exactly preserves the custom non-uniform geometry from the reference mockup:
/// - Asymmetrical low crossbar on `H` (Row 2)
/// - Short 2-unit middle arm on `E` (Row 2)
/// - Center-pillar block `X`
///
/// ```text
/// ██  ██  ██████  ██  ██
/// ██  ██  ██        ██  
/// ██████  ████      ██  
/// ██  ██  ██████  ██  ██
/// ```
#[derive(Debug, Clone, Copy)]
pub struct StartWordmark {
    pub style: Style,
}

impl StartWordmark {
    pub const HEIGHT: u16 = 4;
    pub const WIDTH: u16 = 22;

    pub fn new(style: Style) -> Self {
        Self { style }
    }
}

impl Widget for StartWordmark {
    fn render(self, area: Rect, buf: &mut Buffer) {
        if area.height < Self::HEIGHT || area.width < Self::WIDTH {
            return;
        }

        let lines = [
            "██  ██  ██████  ██  ██",
            "██  ██  ██        ██  ",
            "██████  ████      ██  ",
            "██  ██  ██████  ██  ██",
        ];

        for (i, line) in lines.iter().enumerate() {
            let y = area.y + i as u16;
            buf.set_string(area.x, y, line, self.style);
        }
    }
}

/// Compact 2-row HEX Agent wordmark for the Active Session transcript header.
///
/// ```text
/// █ █ █▀▀ ▀▄▀
/// █▀█ ██▄ ▄▀▄
/// ```
#[derive(Debug, Clone, Copy)]
pub struct SessionWordmark {
    pub style: Style,
}

impl SessionWordmark {
    pub const HEIGHT: u16 = 2;
    pub const WIDTH: u16 = 15;

    pub fn new(style: Style) -> Self {
        Self { style }
    }
}

impl Widget for SessionWordmark {
    fn render(self, area: Rect, buf: &mut Buffer) {
        if area.height < Self::HEIGHT || area.width < Self::WIDTH {
            return;
        }

        let lines = [
            "█ █ █▀▀ ▀▄▀",
            "█▀█ ██▄ ▄▀▄",
        ];

        for (i, line) in lines.iter().enumerate() {
            let y = area.y + i as u16;
            buf.set_string(area.x, y, line, self.style);
        }
    }
}
