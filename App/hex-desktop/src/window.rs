//! Window configuration for HEX Desktop (1440×900 opaque mockup canvas).

use gpui::*;

/// Keep the initial window clear of screen edges, the menu bar, and the Dock.
pub const DISPLAY_MARGIN: f32 = 24.0;

/// Window dimensions and macOS Metal acceleration configuration.
#[derive(Debug, Clone)]
pub struct WindowConfig {
    /// Window width in pixels.
    pub width: Pixels,
    /// Window height in pixels.
    pub height: Pixels,
    /// Minimum allowed window width.
    pub min_width: Pixels,
    /// Minimum allowed window height.
    pub min_height: Pixels,
    /// Traffic light horizontal offset.
    pub traffic_light_x: Pixels,
    /// Traffic light vertical offset.
    pub traffic_light_y: Pixels,
    /// Window title.
    pub title: Option<String>,
}

impl Default for WindowConfig {
    fn default() -> Self {
        Self {
            width: px(1440.0),
            height: px(900.0),
            min_width: px(900.0),
            min_height: px(600.0),
            traffic_light_x: px(14.0),
            traffic_light_y: px(16.0),
            title: Some("HEX Autonomous AI Workbench".to_string()),
        }
    }
}

impl WindowConfig {
    /// Returns default centered window bounds for a standard 1920×1080 display.
    pub fn default_bounds(&self) -> Bounds<Pixels> {
        self.centered_on_display(Bounds {
            origin: point(px(0.0), px(0.0)),
            size: size(px(1920.0), px(1080.0)),
        })
    }

    /// Fits and centers the initial window on a specific display.
    pub fn centered_on_display(&self, display: Bounds<Pixels>) -> Bounds<Pixels> {
        let margin = px(DISPLAY_MARGIN * 2.0);
        let available = size(
            (display.size.width - margin).max(px(1.0)),
            (display.size.height - margin).max(px(1.0)),
        );
        let window_size = size(
            self.width.min(available.width),
            self.height.min(available.height),
        );
        let origin_x = display.origin.x + (display.size.width - window_size.width) / 2.0;
        let origin_y = display.origin.y + (display.size.height - window_size.height) / 2.0;
        Bounds {
            origin: point(origin_x, origin_y),
            size: window_size,
        }
    }

    /// Titlebar options with translucent background and custom traffic light placement.
    pub fn titlebar_options(&self) -> TitlebarOptions {
        TitlebarOptions {
            title: self.title.clone().map(Into::into),
            appears_transparent: true,
            traffic_light_position: Some(point(self.traffic_light_x, self.traffic_light_y)),
        }
    }

    /// Construct `WindowOptions` for native macOS Metal rendering with the
    /// opaque mockup canvas (no vibrancy).
    pub fn window_options(&self, bounds: Option<Bounds<Pixels>>) -> WindowOptions {
        let b = bounds.unwrap_or_else(|| self.default_bounds());
        WindowOptions {
            window_bounds: Some(WindowBounds::Windowed(b)),
            titlebar: Some(self.titlebar_options()),
            window_background: WindowBackgroundAppearance::Opaque,
            window_min_size: Some(size(
                self.min_width.min(b.size.width),
                self.min_height.min(b.size.height),
            )),
            focus: true,
            show: true,
            ..Default::default()
        }
    }
}
