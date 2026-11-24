//! Mockup motion primitives (parity plan §2.6). Each helper takes an elapsed
//! `Duration` since the animation started and returns the instantaneous value,
//! so callers only track a start instant. All periodic helpers wrap their own
//! period. Every consumer must gate animation on `reduced_motion()`.

use std::time::Duration;

/// Glow halo breathing: 5s ease-in-out, opacity .32 → .7 → .32.
pub const BREATHE_PERIOD: Duration = Duration::from_millis(5000);
pub const BREATHE_MIN: f32 = 0.32;
pub const BREATHE_MAX: f32 = 0.7;

/// Composer caret blink: 1.1s steps(1), visible through 55% of the cycle.
pub const BLINK_PERIOD: Duration = Duration::from_millis(1100);
pub const BLINK_ON_FRACTION: f32 = 0.56;

/// Pulsing accent dots (bell badge, running rows): 2s ease-in-out, .35 → 1 → .35.
pub const PULSE_PERIOD: Duration = Duration::from_millis(2000);
pub const PULSE_MIN: f32 = 0.35;

/// Running-session spinner: .9s steps(1) over 10 braille frames, 1ch wide.
pub const SPIN_PERIOD: Duration = Duration::from_millis(900);
pub const SPINNER_FRAMES: [char; 10] = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'];

/// Entrance rise: 0.9s cubic-bezier(.2,.7,.2,1).
pub const RISE_DURATION: Duration = Duration::from_millis(900);

/// CSS `ease-in-out` keyword as a cubic bezier.
const EASE_IN_OUT: (f32, f32, f32, f32) = (0.42, 0.0, 0.58, 1.0);

/// One scalar axis of a cubic-bezier(0, 0, x1, y1, x2, y2, 1, 1) curve.
fn bezier_axis(t: f32, c1: f32, c2: f32) -> f32 {
    let u = 1.0 - t;
    3.0 * u * u * t * c1 + 3.0 * u * t * t * c2 + t * t * t
}

/// Evaluates a CSS cubic-bezier timing function at progress `x` ∈ [0, 1] via
/// bisection (the x axis is monotonic for curves with x1,x2 ∈ [0, 1]).
pub fn cubic_bezier(x1: f32, y1: f32, x2: f32, y2: f32, x: f32) -> f32 {
    if x <= 0.0 {
        return 0.0;
    }
    if x >= 1.0 {
        return 1.0;
    }
    let mut lo = 0.0f32;
    let mut hi = 1.0f32;
    for _ in 0..32 {
        let mid = (lo + hi) / 2.0;
        if bezier_axis(mid, x1, x2) < x {
            lo = mid;
        } else {
            hi = mid;
        }
    }
    bezier_axis((lo + hi) / 2.0, y1, y2)
}

/// `rise` entrance progress (translate/opacity) at the given elapsed time.
pub fn rise_progress(elapsed: Duration) -> f32 {
    let x = (elapsed.as_secs_f32() / RISE_DURATION.as_secs_f32()).clamp(0.0, 1.0);
    cubic_bezier(0.2, 0.7, 0.2, 1.0, x)
}

/// Triangle phase: 0 → 1 → 0 across the period, eased with CSS ease-in-out
/// per keyframe segment (the mockup's `ease-in-out infinite` alternating pairs).
fn eased_triangle_opacity(elapsed: Duration, period: Duration, min: f32, max: f32) -> f32 {
    let phase = (elapsed.as_secs_f32() / period.as_secs_f32()) % 1.0;
    let (seg, dir) = if phase <= 0.5 { (phase * 2.0, 1.0) } else { ((phase - 0.5) * 2.0, -1.0) };
    let e = cubic_bezier(EASE_IN_OUT.0, EASE_IN_OUT.1, EASE_IN_OUT.2, EASE_IN_OUT.3, seg);
    min + (max - min) * (if dir > 0.0 { e } else { 1.0 - e })
}

/// Glow halo opacity at the given elapsed time (range `.32..=.7`).
pub fn breathe_opacity(elapsed: Duration) -> f32 {
    eased_triangle_opacity(elapsed, BREATHE_PERIOD, BREATHE_MIN, BREATHE_MAX)
}

/// Pulsing accent dot opacity at the given elapsed time (range `.35..=1.0`).
pub fn pulse_opacity(elapsed: Duration) -> f32 {
    eased_triangle_opacity(elapsed, PULSE_PERIOD, PULSE_MIN, 1.0)
}

/// Caret visibility at the given elapsed time (on through 55% of the cycle).
pub fn blink_visible(elapsed: Duration) -> bool {
    let ms = elapsed.as_millis() as u64 % BLINK_PERIOD.as_millis() as u64;
    (ms as f32 / BLINK_PERIOD.as_millis() as f32) < BLINK_ON_FRACTION
}

/// Spinner glyph at the given elapsed time (steps(1), 90ms per frame).
pub fn spinner_frame(elapsed: Duration) -> char {
    let ms = elapsed.as_millis() as u64 % SPIN_PERIOD.as_millis() as u64;
    SPINNER_FRAMES[(ms / 90) as usize % SPINNER_FRAMES.len()]
}

#[cfg(test)]
mod tests {
    use super::*;
    use core::prelude::v1::test;

    #[test]
    fn test_blink_duty_quantization() {
        // steps(1): fully on through 55% of the 1.1s cycle, fully off from 56%.
        assert!(blink_visible(Duration::from_millis(0)));
        assert!(blink_visible(Duration::from_millis(605))); // 55%
        assert!(!blink_visible(Duration::from_millis(616))); // 56%
        assert!(!blink_visible(Duration::from_millis(1099)));
        assert!(blink_visible(Duration::from_millis(1100))); // wraps
        assert!(blink_visible(Duration::from_millis(12_100))); // wraps many times
    }

    #[test]
    fn test_spin_frame_sequence() {
        const EXPECTED: [char; 10] = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'];
        assert_eq!(SPINNER_FRAMES, EXPECTED);
        for (i, expected) in EXPECTED.iter().enumerate() {
            assert_eq!(spinner_frame(Duration::from_millis(i as u64 * 90)), *expected);
        }
        // steps(1): the last frame holds through the end of the period, then wraps.
        assert_eq!(spinner_frame(Duration::from_millis(899)), '⠏');
        assert_eq!(spinner_frame(Duration::from_millis(900)), '⠋');
        assert_eq!(spinner_frame(Duration::from_millis(9_000)), '⠋');
    }

    #[test]
    fn test_bezier_endpoints_and_monotonic_rise() {
        assert_eq!(cubic_bezier(0.2, 0.7, 0.2, 1.0, 0.0), 0.0);
        assert_eq!(cubic_bezier(0.2, 0.7, 0.2, 1.0, 1.0), 1.0);
        assert_eq!(cubic_bezier(EASE_IN_OUT.0, EASE_IN_OUT.1, EASE_IN_OUT.2, EASE_IN_OUT.3, 0.0), 0.0);
        assert_eq!(cubic_bezier(EASE_IN_OUT.0, EASE_IN_OUT.1, EASE_IN_OUT.2, EASE_IN_OUT.3, 1.0), 1.0);

        let mut prev = 0.0;
        for step in 0..=100u32 {
            let x = step as f32 / 100.0;
            let y = rise_progress(Duration::from_secs_f32(x * RISE_DURATION.as_secs_f32()));
            assert!((0.0..=1.0).contains(&y));
            assert!(y >= prev - 1e-6, "rise regressed at {x}: {y} < {prev}");
            prev = y;
        }
        // The (.2,.7,.2,1) curve overshoots linear pace early (fast start).
        assert!(rise_progress(Duration::from_secs_f32(0.25 * RISE_DURATION.as_secs_f32())) > 0.5);
    }

    #[test]
    fn test_breathe_range_and_endpoints() {
        assert!((breathe_opacity(Duration::from_millis(0)) - BREATHE_MIN).abs() < 1e-6);
        assert!((breathe_opacity(Duration::from_millis(2500)) - BREATHE_MAX).abs() < 1e-6);
        assert!((breathe_opacity(Duration::from_millis(5000)) - BREATHE_MIN).abs() < 1e-6);
        assert!((breathe_opacity(Duration::from_millis(7500)) - BREATHE_MAX).abs() < 1e-6);
        assert!((breathe_opacity(Duration::from_millis(10_000)) - BREATHE_MIN).abs() < 1e-6);
        for step in 0..=100u32 {
            let t = Duration::from_millis(step as u64 * 50);
            let o = breathe_opacity(t);
            assert!(
                (BREATHE_MIN - 1e-6..=BREATHE_MAX + 1e-6).contains(&o),
                "breathe out of range at {t:?}: {o}"
            );
        }
    }

    #[test]
    fn test_pulse_range() {
        assert!((pulse_opacity(Duration::from_millis(0)) - PULSE_MIN).abs() < 1e-6);
        assert!((pulse_opacity(Duration::from_millis(1000)) - 1.0).abs() < 1e-6);
        assert!((pulse_opacity(Duration::from_millis(3000)) - 1.0).abs() < 1e-6);
        for step in 0..=100u32 {
            let o = pulse_opacity(Duration::from_millis(step as u64 * 20));
            assert!((PULSE_MIN - 1e-6..=1.0 + 1e-6).contains(&o));
        }
    }
}
