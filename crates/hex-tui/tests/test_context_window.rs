use hex_tui::app::{resolve_fallback_model_context, App};
use hex_tui::ui::right_rail::format_tokens;

#[test]
fn test_resolve_fallback_model_context() {
    assert_eq!(resolve_fallback_model_context("gemma-4-31b-it"), 256_000);
    assert_eq!(resolve_fallback_model_context("gemma-4-26b-a4b-it"), 256_000);
    assert_eq!(resolve_fallback_model_context("gemma4:31b"), 256_000);
    assert_eq!(resolve_fallback_model_context("gemini-2.5-pro"), 1_048_576);
    assert_eq!(resolve_fallback_model_context("gemini-2.5-flash"), 1_048_576);
    assert_eq!(resolve_fallback_model_context("gemini-3.8-flash-tiered"), 1_048_576);
    assert_eq!(resolve_fallback_model_context("claude-3-5-sonnet-20241022"), 200_000);
    assert_eq!(resolve_fallback_model_context("claude-sonnet-4-6"), 1_000_000);
    assert_eq!(resolve_fallback_model_context("gpt-4o"), 128_000);
    assert_eq!(resolve_fallback_model_context("deepseek-chat"), 1_000_000);
    assert_eq!(resolve_fallback_model_context("llama-3.3-70b-instruct"), 131_072);
    assert_eq!(resolve_fallback_model_context("grok-4.6"), 500_000);
    assert_eq!(resolve_fallback_model_context("kimi-k3"), 1_048_576);
    assert_eq!(resolve_fallback_model_context("solar-mini"), 32_768);
    assert_eq!(resolve_fallback_model_context("unknown-model"), 256_000);
}

#[test]
fn test_format_tokens() {
    assert_eq!(format_tokens(0), "0k");
    assert_eq!(format_tokens(12_400), "12k");
    assert_eq!(format_tokens(200_000), "200k");
    assert_eq!(format_tokens(256_000), "256k");
    assert_eq!(format_tokens(1_000_000), "1M");
    assert_eq!(format_tokens(1_048_576), "1.0M");
    assert_eq!(format_tokens(2_097_152), "2.1M");
}

#[test]
fn test_app_initialization_with_model_context_and_live_updates() {
    let mut app = App::new("test-project", "gemma-4-31b-it", "OverlayFS");
    assert_eq!(app.max_tokens, 256_000, "Gemma 4 31B must initialize to 256k context");

    // Live update via ACP / handshake
    app.set_max_tokens(1_048_576);
    assert_eq!(app.max_tokens, 1_048_576, "Should allow updating max context window dynamically");
}
