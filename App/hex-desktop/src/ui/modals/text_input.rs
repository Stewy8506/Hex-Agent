use std::ops::Range;

use gpui::prelude::{FluentBuilder, InteractiveElement};
use gpui::*;
use unicode_segmentation::UnicodeSegmentation;

use crate::theme::HermesTheme;

pub type ModalClickHandler = std::rc::Rc<
    dyn Fn(&ClickEvent, &mut Window, &mut App),
>;

pub fn boundary(value: &str, offset: usize) -> usize {
    let mut offset = offset.min(value.len());
    while !value.is_char_boundary(offset) {
        offset -= 1;
    }
    offset
}

pub fn selected_range(value: &str, cursor: usize, anchor: usize) -> Range<usize> {
    let cursor = boundary(value, cursor);
    let anchor = boundary(value, anchor);
    if cursor <= anchor {
        cursor..anchor
    } else {
        anchor..cursor
    }
}

pub fn selected_text(value: &str, cursor: usize, anchor: usize) -> String {
    value[selected_range(value, cursor, anchor)].to_string()
}

pub fn replace_selection(
    value: &mut String,
    cursor: &mut usize,
    anchor: &mut usize,
    text: &str,
) -> Range<usize> {
    let range = selected_range(value, *cursor, *anchor);
    value.replace_range(range.clone(), text);
    let next = range.start + text.len();
    *cursor = next;
    *anchor = next;
    range
}

pub fn insert_text(value: &mut String, cursor: &mut usize, anchor: &mut usize, text: &str) -> Range<usize> {
    replace_selection(value, cursor, anchor, text)
}

pub fn previous_grapheme(value: &str, offset: usize) -> usize {
    let offset = boundary(value, offset);
    value
        .grapheme_indices(true)
        .rev()
        .find_map(|(index, _)| (index < offset).then_some(index))
        .unwrap_or(0)
}

pub fn next_grapheme(value: &str, offset: usize) -> usize {
    let offset = boundary(value, offset);
    value
        .grapheme_indices(true)
        .find_map(|(index, _)| (index > offset).then_some(index))
        .unwrap_or(value.len())
}

pub fn backspace(value: &mut String, cursor: &mut usize, anchor: &mut usize) -> bool {
    let range = selected_range(value, *cursor, *anchor);
    if !range.is_empty() {
        replace_selection(value, cursor, anchor, "");
        return true;
    }
    let previous = previous_grapheme(value, *cursor);
    if previous == *cursor {
        return false;
    }
    value.replace_range(previous..*cursor, "");
    *cursor = previous;
    *anchor = previous;
    true
}

pub fn delete(value: &mut String, cursor: &mut usize, anchor: &mut usize) -> bool {
    let range = selected_range(value, *cursor, *anchor);
    if !range.is_empty() {
        replace_selection(value, cursor, anchor, "");
        return true;
    }
    let next = next_grapheme(value, *cursor);
    if next == *cursor {
        return false;
    }
    value.replace_range(*cursor..next, "");
    true
}

pub fn move_horizontal(
    value: &str,
    cursor: &mut usize,
    anchor: &mut usize,
    delta: isize,
    selecting: bool,
) {
    let current = boundary(value, *cursor);
    let next = if delta < 0 {
        previous_grapheme(value, current)
    } else {
        next_grapheme(value, current)
    };
    if selecting {
        *cursor = next;
    } else {
        let target = if selected_range(value, current, *anchor).is_empty() {
            next
        } else if delta < 0 {
            selected_range(value, current, *anchor).start
        } else {
            selected_range(value, current, *anchor).end
        };
        *cursor = target;
        *anchor = target;
    }
}

pub fn move_to(value: &str, cursor: &mut usize, anchor: &mut usize, offset: usize, selecting: bool) {
    let offset = boundary(value, offset);
    if selecting {
        *cursor = offset;
    } else {
        *cursor = offset;
        *anchor = offset;
    }
}

pub fn move_to_edge(value: &str, cursor: &mut usize, anchor: &mut usize, end: bool, selecting: bool) {
    move_to(value, cursor, anchor, if end { value.len() } else { 0 }, selecting);
}

pub fn select_all(value: &str, cursor: &mut usize, anchor: &mut usize) {
    *anchor = 0;
    *cursor = value.len();
}

fn caret(accent: Rgba) -> Div {
    div()
        .w(px(1.0))
        .h(px(16.0))
        .bg(accent)
        .flex_none()
}

pub fn input_content(
    value: &str,
    cursor: usize,
    anchor: usize,
    placeholder: &str,
    password: bool,
    focused: bool,
    theme: &HermesTheme,
) -> Div {
    let row = div().flex().flex_row().items_center().min_w_0().flex_1();
    if value.is_empty() {
        return row
            .text_color(theme.mockup_text_dim)
            .child(placeholder.to_string())
            .when(focused, |this| this.child(caret(theme.accent_blue())));
    }
    if password {
        let graphemes: Vec<&str> = value.grapheme_indices(true).map(|(_, g)| g).collect();
        let cursor_grapheme = value[..boundary(value, cursor)].graphemes(true).count();
        return row
            .text_color(theme.mockup_text)
            .child("•".repeat(cursor_grapheme))
            .when(focused, |this| this.child(caret(theme.accent_blue())))
            .child("•".repeat(graphemes.len().saturating_sub(cursor_grapheme)));
    }

    let range = selected_range(value, cursor, anchor);
    if range.is_empty() {
        let cursor = boundary(value, cursor);
        return row
            .text_color(theme.mockup_text)
            .child(value[..cursor].to_string())
            .when(focused, |this| this.child(caret(theme.accent_blue())))
            .child(value[cursor..].to_string());
    }

    row.text_color(theme.mockup_text)
        .child(value[..range.start].to_string())
        .child(
            div()
                .bg(rgba(0x7aa7ff40))
                .rounded(px(2.0))
                .child(value[range.clone()].to_string()),
        )
        .child(value[range.end..].to_string())
}

pub fn text_field(
    label: &str,
    value: &str,
    cursor: usize,
    anchor: usize,
    placeholder: &str,
    password: bool,
    focused: bool,
    disabled: bool,
    theme: &HermesTheme,
    on_click: Option<ModalClickHandler>,
) -> Stateful<Div> {
    let field_id = format!(
        "modal_field_{}",
        label
            .chars()
            .map(|ch| if ch.is_ascii_alphanumeric() { ch.to_ascii_lowercase() } else { '_' })
            .collect::<String>()
            .trim_matches('_')
    );
    let mut field = div()
        .id(SharedString::from(field_id.clone()))
        .debug_selector(move || field_id)
        .w_full()
        .flex()
        .flex_col()
        .gap(px(5.0))
        .child(
            div()
                .text_size(px(11.0))
                .font_weight(FontWeight::MEDIUM)
                .text_color(theme.mockup_text_muted)
                .child(label.to_string()),
        )
        .child(
            div()
                .min_h(px(38.0))
                .w_full()
                .flex()
                .items_center()
                .px(px(10.0))
                .py(px(8.0))
                .rounded(px(8.0))
                .bg(theme.mockup_sidebar)
                .border_1()
                .border_color(if focused {
                    theme.accent_blue
                } else {
                    theme.mockup_border
                })
                .when(focused, |this| this.shadow_sm())
                .font_family(theme.font_mono()[0].clone())
                .text_size(px(13.0))
                .cursor_pointer()
                .child(input_content(
                    value,
                    cursor,
                    anchor,
                    placeholder,
                    password,
                    focused && !disabled,
                    theme,
                )),
        );
    if disabled {
        field = field.opacity(0.55);
    }
    if let Some(on_click) = on_click {
        field.interactivity().on_click(move |event, window, cx| {
            on_click(event, window, cx)
        });
    }
    field
}

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum ButtonTone {
    Neutral,
    Accent,
    Destructive,
}

pub fn button(
    label: &str,
    tone: ButtonTone,
    enabled: bool,
    focused: bool,
    theme: &HermesTheme,
    on_click: ModalClickHandler,
) -> Stateful<Div> {
    let (background, foreground, border) = match tone {
        ButtonTone::Neutral => (theme.mockup_hover, theme.mockup_text, theme.mockup_border_strong),
        ButtonTone::Accent => (theme.accent_blue, gpui::rgb(0xffffff), theme.accent_blue),
        ButtonTone::Destructive => (theme.mockup_hover, theme.rose_fg(), theme.rose_fg()),
    };
    let button_id = format!(
        "modal_button_{}",
        label
            .chars()
            .map(|ch| if ch.is_ascii_alphanumeric() { ch.to_ascii_lowercase() } else { '_' })
            .collect::<String>()
            .trim_matches('_')
    );
    div()
        .id(SharedString::from(button_id.clone()))
        .debug_selector(move || button_id)
        .min_w(px(84.0))
        .h(px(34.0))
        .flex()
        .items_center()
        .justify_center()
        .px(px(12.0))
        .rounded(px(8.0))
        .bg(background)
        .text_color(foreground)
        .border_1()
        .border_color(if focused { foreground } else { border })
        .when(focused, |this| this.shadow_sm())
        .when(enabled, |mut this| {
            this = this.cursor_pointer().hover(|this| this.opacity(0.88));
            this.interactivity().on_click(move |event, window, cx| {
                on_click(event, window, cx)
            });
            this
        })
        .when(!enabled, |this| this.opacity(0.45))
        .text_size(px(12.0))
        .font_weight(FontWeight::MEDIUM)
        .child(label.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use core::prelude::v1::test;

    #[test]
    fn selection_replacement_and_grapheme_deletion() {
        let mut value = "a👩\u{200d}💻b".to_string();
        let mut cursor = value.len();
        let mut anchor = cursor;
        move_horizontal(&value, &mut cursor, &mut anchor, -1, false);
        assert_eq!(cursor, "a👩\u{200d}💻".len());
        backspace(&mut value, &mut cursor, &mut anchor);
        assert_eq!(value, "ab");

        move_to(&value, &mut cursor, &mut anchor, 0, false);
        move_horizontal(&value, &mut cursor, &mut anchor, 1, true);
        insert_text(&mut value, &mut cursor, &mut anchor, "x");
        assert_eq!(value, "xb");
        assert_eq!(selected_text(&value, cursor, anchor), "");
    }

    #[test]
    fn selection_collapses_at_the_nearest_edge() {
        let value = "hello".to_string();
        let mut cursor = 5;
        let mut anchor = 1;
        move_horizontal(&value, &mut cursor, &mut anchor, -1, false);
        assert_eq!((cursor, anchor), (1, 1));
        move_horizontal(&value, &mut cursor, &mut anchor, 1, false);
        assert_eq!((cursor, anchor), (2, 2));
    }
}
