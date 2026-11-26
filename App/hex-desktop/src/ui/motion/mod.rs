mod ambient;
mod interaction;
mod lerp;
mod presets;
mod reduced;
mod spring;
mod system;

pub use ambient::{
    blink_visible, breathe_opacity, cubic_bezier, pulse_opacity, rise_progress, spinner_frame,
    BLINK_ON_FRACTION, BLINK_PERIOD, BREATHE_MAX, BREATHE_MIN, BREATHE_PERIOD, PULSE_MIN,
    PULSE_PERIOD, RISE_DURATION, SPINNER_FRAMES, SPIN_PERIOD,
};
pub use interaction::{
    hover_feedback, press_feedback, slot, ControlMotion, ControlValues, RevealMotion, RevealValues,
    CONTROL_PRESS_OFFSET, CONTROL_PRESS_OPACITY, REVEAL_RISE,
};
pub use lerp::{lerp, lerp_hsla, lerp_px};
pub use presets::{spring_easing, SpringPreset, BOUNCY, SMOOTH, SNAPPY};
pub use reduced::{
    reduced_motion, MotionKind, MotionPolicy, MotionPreference, REDUCED_OPACITY_FADE,
};
pub use spring::{Spring, SETTLE_POSITION_EPSILON, SETTLE_VELOCITY_EPSILON};
pub use system::{MotionSystem, SpringKey};
