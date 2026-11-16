//! Dynamic animation engine for the HEX Agent Cockpit.
//!
//! Provides tick-driven Braille spinners, 2.2s sine pulsing active step markers,
//! 550ms cursor blinking, and moving gradient shimmer text for thinking states.

use ratatui::style::{Color, Modifier, Style};
use ratatui::text::Span;
use std::time::{Duration, Instant};

/// 10-step Braille spinner sequence exactly matching the HTML mockup (`.sp::before`).
pub const BRAILLE_FRAMES: [&str; 10] = [
    "⠋", // 0%
    "⠙", // 10%
    "⠹", // 20%
    "⠸", // 30%
    "⠼", // 40%
    "⠴", // 50%
    "⠦", // 60%
    "⠧", // 70%
    "⠇", // 80%
    "⠿", // 90%
];

/// Animated state driven by a monotonically increasing tick or clock.
#[derive(Debug, Clone)]
pub struct AnimationState {
    start_time: Instant,
}

impl Default for AnimationState {
    fn default() -> Self {
        Self::new()
    }
}

impl AnimationState {
    pub fn new() -> Self {
        Self {
            start_time: Instant::now(),
        }
    }

    /// Construct animation state from an existing clock / start instant.
    pub fn with_start(start_time: Instant) -> Self {
        Self { start_time }
    }

    /// Elapsed time since animation engine started.
    pub fn elapsed(&self) -> Duration {
        self.start_time.elapsed()
    }

    /// Returns the current Braille spinner glyph (advances every 90ms).
    pub fn spinner_frame(&self) -> &'static str {
        let ms = self.elapsed().as_millis();
        let frame_index = ((ms / 90) % (BRAILLE_FRAMES.len() as u128)) as usize;
        BRAILLE_FRAMES[frame_index]
    }

    /// Returns whether the text cursor should be visible (1.1s cycle, 55% duty cycle: 605ms on, 495ms off).
    pub fn cursor_visible(&self) -> bool {
        let ms = self.elapsed().as_millis() % 1100;
        ms < 605
    }

    /// Calculates a pulse progress value between 0.35 and 1.0 using a 2.2-second sine cycle.
    pub fn pulse_factor(&self) -> f32 {
        let secs = self.elapsed().as_secs_f32();
        let cycle = 2.2;
        // sine wave mapped from [-1, 1] to [0.35, 1.0]
        let norm_sin = ((secs * 2.0 * std::f32::consts::PI / cycle).sin() + 1.0) / 2.0;
        0.35 + 0.65 * norm_sin
    }

    /// Returns the color of the active pulsing step marker `[>]`.
    ///
    /// Interpolates between `dim` and `accent` based on `pulse_factor`.
    pub fn pulse_color(&self, dim: Color, accent: Color) -> Color {
        let factor = self.pulse_factor();
        interpolate_color(dim, accent, factor)
    }

    /// Returns the active placeholder prompt phrase (rotates every 5 seconds).
    pub fn placeholder_prompt(&self) -> &'static str {
        const PROMPTS: [&str; 3] = [
            "Describe what to build or fix",
            "Fix the failing test in src/",
            "Explain how this repo is structured",
        ];
        let idx = (self.elapsed().as_secs() / 5) as usize % PROMPTS.len();
        PROMPTS[idx]
    }

    /// Generates spans with a shimmering gradient sweep across a thinking line.
    ///
    /// The sweep runs across text on a 2.4s linear cycle.
    pub fn render_shimmer<'a>(&self, text: &'a str, base_color: Color, highlight_color: Color) -> Vec<Span<'a>> {
        let chars: Vec<char> = text.chars().collect();
        if chars.is_empty() {
            return vec![];
        }

        let total_chars = chars.len();
        let secs = self.elapsed().as_secs_f32();
        let cycle = 2.4;
        let progress = (secs % cycle) / cycle;

        // Position of the sweep center relative to text length
        let center_pos = progress * (total_chars as f32 + 10.0) - 5.0;
        let band_width = 6.0_f32;

        let mut spans = Vec::with_capacity(chars.len());
        for (i, c) in chars.into_iter().enumerate() {
            let dist = ((i as f32) - center_pos).abs();
            let factor = if dist < band_width {
                1.0 - (dist / band_width)
            } else {
                0.0
            };

            let color = if factor > 0.05 {
                interpolate_color(base_color, highlight_color, factor)
            } else {
                base_color
            };

            let mut style = Style::default().fg(color);
            if factor > 0.7 {
                style = style.add_modifier(Modifier::BOLD);
            }
            spans.push(Span::styled(c.to_string(), style));
        }

        spans
    }
}

/// Applies a soft top-fade mask to the topmost rows of a scrollable transcript buffer.
///
/// Interpolates text colors toward the background color (`#0e0e0e`), matching the
/// `-webkit-mask-image: linear-gradient(to bottom, transparent 0, #000 16px)` from the HTML mockup.
pub fn apply_top_fade(area: ratatui::layout::Rect, fade_rows: u16, bg_color: Color, buf: &mut ratatui::buffer::Buffer) {
    let max_rows = fade_rows.min(area.height);
    for row_idx in 0..max_rows {
        let y = area.y + row_idx;
        if y >= area.bottom() {
            break;
        }
        // Factor ramps from ~0.25 on row 0 to ~0.70 on row 1
        let factor = (row_idx as f32 + 0.3) / (fade_rows as f32 + 0.3);
        for x in area.left()..area.right() {
            let cell = &mut buf[(x, y)];
            let fg = cell.fg;
            cell.set_fg(interpolate_color(bg_color, fg, factor));
        }
    }
}

/// Linear interpolation between two colors.
pub fn interpolate_color(c1: Color, c2: Color, factor: f32) -> Color {
    let t = factor.clamp(0.0, 1.0);
    match (c1, c2) {
        (Color::Rgb(r1, g1, b1), Color::Rgb(r2, g2, b2)) => {
            let r = ((1.0 - t) * (r1 as f32) + t * (r2 as f32)).round() as u8;
            let g = ((1.0 - t) * (g1 as f32) + t * (g2 as f32)).round() as u8;
            let b = ((1.0 - t) * (b1 as f32) + t * (b2 as f32)).round() as u8;
            Color::Rgb(r, g, b)
        }
        _ => {
            if t > 0.5 {
                c2
            } else {
                c1
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_braille_spinner_advances() {
        let anim = AnimationState::new();
        let frame = anim.spinner_frame();
        assert!(BRAILLE_FRAMES.contains(&frame));
    }

    #[test]
    fn test_pulse_bounds() {
        let anim = AnimationState::new();
        let p = anim.pulse_factor();
        assert!((0.35..=1.0).contains(&p), "Pulse factor {} out of bounds", p);
    }

    #[test]
    fn test_color_interpolation() {
        let black = Color::Rgb(0, 0, 0);
        let white = Color::Rgb(100, 100, 100);
        let mid = interpolate_color(black, white, 0.5);
        assert_eq!(mid, Color::Rgb(50, 50, 50));
    }

    #[test]
    fn test_shimmer_spans_generation() {
        let anim = AnimationState::new();
        let spans = anim.render_shimmer(
            "Investigating src/graph/builder.ts",
            Color::Rgb(100, 100, 100),
            Color::Rgb(240, 240, 240),
        );
        assert_eq!(spans.len(), "Investigating src/graph/builder.ts".len());
    }
}
