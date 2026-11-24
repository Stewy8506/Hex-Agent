//! Window configuration for HEX Desktop (1440×900 opaque mockup canvas).

use gpui::*;

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
    /// Returns default centered window bounds (calculated for a standard 1920x1080 display).
    pub fn default_bounds(&self) -> Bounds<Pixels> {
        let default_screen_w = px(1920.0);
        let default_screen_h = px(1080.0);
        let origin_x = (default_screen_w - self.width) / 2.0;
        let origin_y = (default_screen_h - self.height) / 2.0;
        Bounds {
            origin: point(origin_x.max(px(0.0)), origin_y.max(px(0.0))),
            size: size(self.width, self.height),
        }
    }

    /// Calculates centered bounds on a specific display bounds rectangle.
    pub fn centered_on_display(&self, display: Bounds<Pixels>) -> Bounds<Pixels> {
        let origin_x = display.origin.x + (display.size.width - self.width) / 2.0;
        let origin_y = display.origin.y + (display.size.height - self.height) / 2.0;
        Bounds {
            origin: point(origin_x.max(display.origin.x), origin_y.max(display.origin.y)),
            size: size(self.width, self.height),
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
            window_min_size: Some(size(self.min_width, self.min_height)),
            focus: true,
            show: true,
            ..Default::default()
        }
    }
}
