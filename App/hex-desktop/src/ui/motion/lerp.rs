use gpui::{px, Hsla, Pixels};

pub fn lerp(start: f32, end: f32, progress: f32) -> f32 {
    start + (end - start) * progress
}

pub fn lerp_px(start: Pixels, end: Pixels, progress: f32) -> Pixels {
    px(lerp(f32::from(start), f32::from(end), progress))
}

pub fn lerp_hsla(start: Hsla, end: Hsla, progress: f32) -> Hsla {
    if progress <= 0.0 {
        return start;
    }
    if progress >= 1.0 {
        return end;
    }
    let mut hue_delta = end.h - start.h;
    if hue_delta > 0.5 {
        hue_delta -= 1.0;
    } else if hue_delta < -0.5 {
        hue_delta += 1.0;
    }
    Hsla {
        h: (start.h + hue_delta * progress).rem_euclid(1.0),
        s: lerp(start.s, end.s, progress),
        l: lerp(start.l, end.l, progress),
        a: lerp(start.a, end.a, progress),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use core::prelude::v1::test;

    #[test]
    fn scalar_and_pixel_interpolation_hit_endpoints() {
        assert_eq!(lerp(10.0, 20.0, 0.0), 10.0);
        assert_eq!(lerp(10.0, 20.0, 0.5), 15.0);
        assert_eq!(lerp(10.0, 20.0, 1.0), 20.0);
        assert_eq!(lerp_px(px(10.0), px(20.0), 0.5), px(15.0));
    }

    #[test]
    fn hsla_uses_shortest_hue_path() {
        let start = Hsla {
            h: 0.9,
            s: 0.2,
            l: 0.3,
            a: 0.4,
        };
        let end = Hsla {
            h: 0.1,
            s: 0.8,
            l: 0.7,
            a: 1.0,
        };
        let middle = lerp_hsla(start, end, 0.5);
        assert!((middle.h - 0.0).abs() < 1e-6);
        assert!((middle.s - 0.5).abs() < 1e-6);
        assert!((middle.l - 0.5).abs() < 1e-6);
        assert!((middle.a - 0.7).abs() < 1e-6);
        let reverse = lerp_hsla(end, start, 0.5).h;
        assert!(reverse.min(1.0 - reverse).abs() < 1e-6);
        assert_eq!(lerp_hsla(start, end, 1.0), end);
    }
}
