//! ASCII block token gauge progress bar and context percentage for HEX Desktop.
//!
//! Formats context token consumption:
//! - Visual progress bar using block characters: `▕██████░░░░░░░░░░▏`
//! - Formatted token counts: e.g. `24.5k / 128k (19%)`
//! - Dynamic threshold styling:
//!   - Normal (< 70%): Muted zinc text (`#71717a`) and electric blue fill (`#7aa7ff`)
//!   - Warning (70–90%): Amber text and fill (`#f59e0b`)
//!   - Critical (> 90%): Rose red text and fill (`#c98b8b`)

use gpui::*;

use crate::theme::HermesTheme;

/// Fallback context window when the runtime does not report one.
pub const DEFAULT_CONTEXT_LIMIT: usize = 128_000;

/// Threshold level for context window consumption.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GaugeThreshold {
    /// Safe usage (< 70%).
    Normal,
    /// Approaching limit (70% - 90%).
    Warning,
    /// Critical context saturation (> 90%).
    Critical,
}

/// Formats a token integer into a compact engineering string (e.g. 24.5k, 128k, 1.2M).
pub fn format_token_number(tokens: usize) -> String {
    if tokens < 1_000 {
        tokens.to_string()
    } else if tokens < 1_000_000 {
        let val = tokens as f64 / 1_000.0;
        if val.fract() == 0.0 || val >= 100.0 {
            format!("{:.0}k", val)
        } else {
            format!("{:.1}k", val)
        }
    } else {
        let val = tokens as f64 / 1_000_000.0;
        if val.fract() == 0.0 {
            format!("{:.0}M", val)
        } else {
            format!("{:.1}M", val)
        }
    }
}

/// Renders an ASCII block progress bar with exact width.
///
/// Uses `▕` as left cap, `█` as filled block, `░` as unfilled block, and `▏` as right cap.
pub fn render_ascii_progress_bar(fraction: f64, width: usize) -> String {
    let clamped = fraction.clamp(0.0, 1.0);
    let filled = (clamped * width as f64).round() as usize;
    let filled = filled.min(width);
    let unfilled = width.saturating_sub(filled);

    let mut bar = String::with_capacity(width * 3 + 6);
    bar.push('▕');
    for _ in 0..filled {
        bar.push('█');
    }
    for _ in 0..unfilled {
        bar.push('░');
    }
    bar.push('▏');
    bar
}

/// Token gauge widget tracking session context usage.
#[derive(Debug, Clone, PartialEq)]
pub struct TokenGaugeWidget {
    pub current_tokens: usize,
    pub max_tokens: usize,
    pub bar_width: usize,
}

impl Default for TokenGaugeWidget {
    fn default() -> Self {
        Self::new(0, DEFAULT_CONTEXT_LIMIT)
    }
}

impl TokenGaugeWidget {
    pub fn new(current_tokens: usize, max_tokens: usize) -> Self {
        Self {
            current_tokens,
            max_tokens: max_tokens.max(1),
            bar_width: 16,
        }
    }

    pub fn with_bar_width(mut self, width: usize) -> Self {
        self.bar_width = width;
        self
    }

    pub fn set_tokens(&mut self, current: usize, max: usize) {
        self.current_tokens = current;
        self.max_tokens = max.max(1);
    }

    pub fn percentage(&self) -> f64 {
        ((self.current_tokens as f64 / self.max_tokens as f64) * 100.0).clamp(0.0, 100.0)
    }

    pub fn fraction(&self) -> f64 {
        (self.current_tokens as f64 / self.max_tokens as f64).clamp(0.0, 1.0)
    }

    pub fn threshold(&self) -> GaugeThreshold {
        let pct = self.percentage();
        if pct < 70.0 {
            GaugeThreshold::Normal
        } else if pct <= 90.0 {
            GaugeThreshold::Warning
        } else {
            GaugeThreshold::Critical
        }
    }

    pub fn threshold_color(&self, theme: &HermesTheme) -> Rgba {
        match self.threshold() {
            GaugeThreshold::Normal => theme.accent_blue(),
            GaugeThreshold::Warning => theme.warning_amber(),
            GaugeThreshold::Critical => theme.rose_fg(),
        }
    }

    pub fn format_display_text(&self) -> String {
        format!(
            "{} / {} ({:.0}%)",
            format_token_number(self.current_tokens),
            format_token_number(self.max_tokens),
            self.percentage()
        )
    }

    pub fn ascii_bar(&self) -> String {
        render_ascii_progress_bar(self.fraction(), self.bar_width)
    }

    /// Renders the token gauge bar and telemetry label.
    pub fn render(&self, theme: &HermesTheme) -> Div {
        let bar_str = self.ascii_bar();
        let display_text = self.format_display_text();
        let color = self.threshold_color(theme);

        div()
            .flex()
            .flex_row()
            .items_center()
            .gap_2()
            .text_xs()
            .child(
                div()
                    .font_weight(FontWeight::BOLD)
                    .text_color(color)
                    .child(bar_str),
            )
            .child(
                div()
                    .text_color(if self.threshold() == GaugeThreshold::Normal {
                        theme.mockup_text_dim
                    } else {
                        color
                    })
                    .child(display_text),
            )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use core::prelude::v1::test;

    #[test]
    fn test_format_token_number() {
        assert_eq!(format_token_number(500), "500");
        assert_eq!(format_token_number(24_500), "24.5k");
        assert_eq!(format_token_number(128_000), "128k");
        assert_eq!(format_token_number(200_000), "200k");
        assert_eq!(format_token_number(1_000_000), "1M");
        assert_eq!(format_token_number(1_500_000), "1.5M");
    }

    #[test]
    fn test_ascii_progress_bar_rendering() {
        // 0%
        let bar_0 = render_ascii_progress_bar(0.0, 10);
        assert_eq!(bar_0, "▕░░░░░░░░░░▏");

        // 50%
        let bar_50 = render_ascii_progress_bar(0.5, 10);
        assert_eq!(bar_50, "▕█████░░░░░▏");

        // 100%
        let bar_100 = render_ascii_progress_bar(1.0, 10);
        assert_eq!(bar_100, "▕██████████▏");
    }

    #[test]
    fn test_gauge_threshold_transitions() {
        let theme = HermesTheme::default();
        let mut gauge = TokenGaugeWidget::new(0, 100_000);

        // 0% -> Normal (<70%)
        assert_eq!(gauge.threshold(), GaugeThreshold::Normal);
        assert_eq!(gauge.threshold_color(&theme), theme.accent_blue());

        // 50% -> Normal (<70%)
        gauge.set_tokens(50_000, 100_000);
        assert_eq!(gauge.threshold(), GaugeThreshold::Normal);
        assert_eq!(gauge.threshold_color(&theme), theme.accent_blue());

        // 75% -> Warning (70-90%)
        gauge.set_tokens(75_000, 100_000);
        assert_eq!(gauge.threshold(), GaugeThreshold::Warning);
        assert_eq!(gauge.threshold_color(&theme), theme.warning_amber());

        // 95% -> Critical (>90%)
        gauge.set_tokens(95_000, 100_000);
        assert_eq!(gauge.threshold(), GaugeThreshold::Critical);
        assert_eq!(gauge.threshold_color(&theme), theme.rose_fg());
    }
}
