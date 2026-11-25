use std::rc::Rc;

use gpui::*;

use crate::theme::{HermesTheme, MockupTypeScale};
use crate::ui::icon_svg;

pub trait ScrollablePayload {
    fn scroll_x(self, id: &str) -> Self;
    fn scroll_y(self, id: &str) -> Self;
    fn scroll_both(self, id: &str) -> Self;
}

impl ScrollablePayload for Div {
    fn scroll_x(mut self, id: &str) -> Self {
        self.interactivity().element_id = Some(ElementId::Name(SharedString::from(id.to_owned())));
        self.interactivity().base_style.overflow.x = Some(Overflow::Scroll);
        self
    }

    fn scroll_y(mut self, id: &str) -> Self {
        self.interactivity().element_id = Some(ElementId::Name(SharedString::from(id.to_owned())));
        self.interactivity().base_style.overflow.y = Some(Overflow::Scroll);
        self
    }

    fn scroll_both(mut self, id: &str) -> Self {
        self.interactivity().element_id = Some(ElementId::Name(SharedString::from(id.to_owned())));
        self.interactivity().base_style.overflow.x = Some(Overflow::Scroll);
        self.interactivity().base_style.overflow.y = Some(Overflow::Scroll);
        self
    }
}

pub type TextCopyHandler = Rc<dyn Fn(&str, &ClickEvent, &mut Window, &mut App)>;

pub const PAYLOAD_MAX_HEIGHT: f32 = 280.0;
pub const PAYLOAD_MAX_LINES: usize = 400;
pub const PAYLOAD_MAX_CHARS: usize = 24_000;

pub fn preview_text(text: &str, max_lines: usize) -> (String, bool) {
    let mut lines = text.lines();
    let mut preview = lines
        .by_ref()
        .take(max_lines)
        .collect::<Vec<_>>()
        .join("\n");
    let mut truncated = lines.next().is_some();
    if preview.len() > PAYLOAD_MAX_CHARS {
        let mut end = PAYLOAD_MAX_CHARS.min(preview.len());
        while !preview.is_char_boundary(end) {
            end -= 1;
        }
        preview.truncate(end);
        truncated = true;
    }
    (preview, truncated)
}

pub fn preview_notice(theme: &HermesTheme) -> Div {
    div()
        .px(px(12.0))
        .py(px(6.0))
        .border_t_1()
        .border_color(theme.mockup_border)
        .text_size(px(MockupTypeScale::HINT_SIZE))
        .text_color(theme.mockup_text_dim)
        .child("Preview truncated — copy for the full payload")
}

pub fn copy_button(
    id: &str,
    text: &str,
    handler: Option<&TextCopyHandler>,
    theme: &HermesTheme,
) -> AnyElement {
    let Some(handler) = handler else {
        return div().into_any_element();
    };
    let handler = handler.clone();
    let text = text.to_string();
    let selector = format!("payload_copy_{id}");
    div()
        .id(SharedString::from(selector.clone()))
        .debug_selector(move || selector)
        .flex_none()
        .flex()
        .flex_row()
        .items_center()
        .gap(px(4.0))
        .px(px(6.0))
        .py(px(3.0))
        .rounded(px(5.0))
        .text_size(px(MockupTypeScale::HINT_SIZE))
        .text_color(theme.mockup_text_dim)
        .cursor_pointer()
        .hover(|this| this.bg(theme.mockup_hover).text_color(theme.mockup_text))
        .on_click(move |event, window, cx| {
            cx.stop_propagation();
            handler(&text, event, window, cx);
        })
        .child(icon_svg("files", 12.0).text_color(theme.mockup_text_dim))
        .child("Copy")
        .into_any_element()
}

#[cfg(test)]
mod tests {
    use super::*;
    use core::prelude::v1::test;

    #[test]
    fn previews_are_bounded_and_utf8_safe() {
        let text = (0..500)
            .map(|index| format!("line {index}"))
            .collect::<Vec<_>>()
            .join("\n");
        let (preview, truncated) = preview_text(&text, 400);
        assert!(truncated);
        assert_eq!(preview.lines().count(), 400);
        assert!(!preview.contains("line 400"));

        let unicode = "👩\u{200d}💻".repeat(10_000);
        let (preview, truncated) = preview_text(&unicode, 400);
        assert!(truncated);
        assert!(preview.len() <= PAYLOAD_MAX_CHARS);
    }

    #[test]
    fn short_payloads_are_not_truncated() {
        assert_eq!(
            preview_text("short\ntext", 400),
            ("short\ntext".into(), false)
        );
    }
}
