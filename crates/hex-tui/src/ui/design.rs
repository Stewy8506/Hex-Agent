//! The HEX visual language.  Keep the palette and art here rather than
//! accepting pre-rendered ANSI from the Python host: Ratatui owns every cell.

use ratatui::style::Color;

#[derive(Clone, Copy)]
pub struct Skin {
    pub border: Color,
    pub heading: Color,
    pub accent: Color,
    pub category: Color,
    pub text: Color,
    pub muted: Color,
    pub success: Color,
    pub warn: Color,
    pub danger: Color,
    pub surface: Color,
    pub gradient: [Color; 6],
    pub prompt: &'static str,
}

pub const AMBER: Skin = Skin {
    border: Color::Rgb(180, 118, 42),
    heading: Color::Rgb(250, 204, 21),
    accent: Color::Rgb(245, 158, 11),
    category: Color::Rgb(186, 135, 58),
    text: Color::Rgb(243, 244, 246),
    muted: Color::Rgb(138, 129, 115),
    success: Color::Rgb(52, 211, 153),
    warn: Color::Rgb(251, 191, 36),
    danger: Color::Rgb(248, 113, 113),
    surface: Color::Rgb(28, 25, 23),
    gradient: [
        Color::Rgb(255, 215, 0),
        Color::Rgb(245, 185, 15),
        Color::Rgb(235, 150, 20),
        Color::Rgb(217, 119, 24),
        Color::Rgb(194, 89, 22),
        Color::Rgb(168, 68, 20),
    ],
    prompt: "⚕",
};

pub const CYBERPUNK: Skin = Skin { border: Color::Rgb(0,195,215), heading: Color::Rgb(0,245,255), accent: Color::Rgb(244,114,182), category: Color::Rgb(56,189,248), text: Color::Rgb(241,245,249), muted: Color::Rgb(100,116,139), success: Color::Rgb(16,185,129), warn: Color::Rgb(250,204,21), danger: Color::Rgb(244,63,94), surface: Color::Rgb(15,23,42), gradient: [Color::Rgb(0,245,255),Color::Rgb(35,205,255),Color::Rgb(95,155,255),Color::Rgb(160,105,255),Color::Rgb(220,65,255),Color::Rgb(255,40,180)], prompt: "⚡" };
pub const MATRIX: Skin = Skin { border: Color::Rgb(16,185,129), heading: Color::Rgb(110,231,183), accent: Color::Rgb(52,211,153), category: Color::Rgb(5,150,105), text: Color::Rgb(209,250,229), muted: Color::Rgb(75,135,105), success: Color::Rgb(52,211,153), warn: Color::Rgb(250,204,21), danger: Color::Rgb(248,113,113), surface: Color::Rgb(6,28,20), gradient: [Color::Rgb(167,243,208),Color::Rgb(110,231,183),Color::Rgb(52,211,153),Color::Rgb(16,185,129),Color::Rgb(5,150,105),Color::Rgb(4,120,87)], prompt: "⌘" };
pub const NORD: Skin = Skin { border: Color::Rgb(129,161,193), heading: Color::Rgb(136,192,208), accent: Color::Rgb(163,190,140), category: Color::Rgb(129,161,193), text: Color::Rgb(236,239,244), muted: Color::Rgb(125,138,161), success: Color::Rgb(163,190,140), warn: Color::Rgb(235,203,139), danger: Color::Rgb(191,97,106), surface: Color::Rgb(46,52,64), gradient: [Color::Rgb(143,188,187),Color::Rgb(136,192,208),Color::Rgb(129,161,193),Color::Rgb(94,129,172),Color::Rgb(76,86,106),Color::Rgb(67,76,94)], prompt: "❄" };
pub const MONO: Skin = Skin { border: Color::Rgb(140,140,140), heading: Color::Rgb(255,255,255), accent: Color::Rgb(220,220,220), category: Color::Rgb(180,180,180), text: Color::Rgb(240,240,240), muted: Color::Rgb(130,130,130), success: Color::Rgb(200,200,200), warn: Color::Rgb(180,180,180), danger: Color::Rgb(255,255,255), surface: Color::Rgb(30,30,30), gradient: [Color::Rgb(255,255,255),Color::Rgb(230,230,230),Color::Rgb(200,200,200),Color::Rgb(170,170,170),Color::Rgb(140,140,140),Color::Rgb(110,110,110)], prompt: ">" };

pub fn skin(name: &str) -> Skin {
    match name.to_ascii_lowercase().as_str() {
        "cyberpunk" => CYBERPUNK,
        "matrix" => MATRIX,
        "nord" => NORD,
        "mono" => MONO,
        _ => AMBER,
    }
}

pub const BANNER: [&str; 6] = [
    "██╗  ██╗███████╗██╗  ██╗          █████╗  ██████╗ ███████╗███╗   ██╗████████╗",
    "██║  ██║██╔════╝╚██╗██╔╝         ██╔══██╗██╔════╝ ██╔════╝████╗  ██║╚══██╔══╝",
    "███████║█████╗   ╚███╔╝  █████╗  ███████║██║  ███╗█████╗  ██╔██╗ ██║   ██║   ",
    "██╔══██║██╔══╝   ██╔██╗  ╚════╝  ██╔══██║██║   ██║██╔══╝  ██║╚██╗██║   ██║   ",
    "██║  ██║███████╗██╔╝ ██╗         ██║  ██║╚██████╔╝███████╗██║ ╚████║   ██║   ",
    "╚═╝  ╚══════╝╚═╝  ╚═╝          ╚════╝  ╚═════╝ ╚══════╝╚═╝  ╚═══╝   ╚═══════╝",
];

pub const TIPS: [&str; 7] = [
    "/mode switches between plan, agent, and code. Tab on an empty prompt cycles.",
    "/diff inspects unified diffs of uncommitted workspace changes.",
    "/proof displays proof-carrying artifacts and verification hashes.",
    "/plan runs read-only architecture planning without mutating files.",
    "/blast visualizes Active Graphify blast radius and callers.",
    "/memory recalls cross-session architecture facts.",
    "/doctor verifies toolchains, sandboxes, CoW stores, and models.",
];

pub const PROMPTS: [&str; 8] = [
    "Ask me anything…",
    "Describe a feature or fix to implement…",
    "Find and fix bugs in this codebase…",
    "Explain the model router…",
    "Refactor a function or write unit tests…",
    "Type /help to see commands…",
    "Inspect AST dependencies and blast radius…",
    "What would you like to build?",
];

pub const SPINNER: [&str; 10] = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
pub const VERBS: [&str; 6] = [
    "Thinking",
    "Analyzing",
    "Exploring",
    "Formulating",
    "Reflecting",
    "Synthesizing",
];
