#pragma once

#include "nlohmann/json.hpp"

#include <map>
#include <string>

inline bool kvmem_chat_template_has_effort(const std::string & source, const std::string & level) {
    return source.find("'" + level + "'") != std::string::npos ||
           source.find("\"" + level + "\"") != std::string::npos;
}

inline std::string kvmem_chat_template_default_effort(const std::string & source) {
    static const char * levels[] = {"xhigh", "high", "medium", "low", "minimal", "max", "ultra"};
    for (const char * level : levels) {
        if (source.find("default('" + std::string(level) + "')") != std::string::npos ||
            source.find("default(\"" + std::string(level) + "\")") != std::string::npos) {
            return level;
        }
    }
    // If the template does not spell out its default, prefer its most capable
    // recognized branch. Qwen3.8's template uses xhigh as its implicit default.
    for (const char * level : levels) {
        if (kvmem_chat_template_has_effort(source, level)) return level;
    }
    return {};
}

// Keep llama.cpp's JSON-serialized kwargs representation. Templates render and
// validate model-specific effort levels; unsupported generic client values are
// mapped to the template's own default when the source is available.
inline bool kvmem_chat_template_override(const nlohmann::json & body, bool & thinking,
        std::map<std::string, std::string> & kwargs, std::string & err,
        const std::string & template_source = {}) {
    using json = nlohmann::json;
    bool enabled = thinking;
    auto merged = kwargs;
    const auto read_enable = [&](const json & obj) {
        const auto it = obj.find("enable_thinking");
        if (it == obj.end()) return true;
        if (!it->is_boolean()) {
            err = "enable_thinking must be a boolean";
            return false;
        }
        enabled = it->get<bool>();
        return true;
    };
    if (!body.is_object()) { err = "request must be a JSON object"; return false; }
    if (body.contains("kvmem") && body["kvmem"].is_object() && !read_enable(body["kvmem"])) return false;
    if (!read_enable(body)) return false;
    const json * effort = nullptr;
    const auto kw = body.find("chat_template_kwargs");
    if (kw != body.end() && !kw->is_null()) {
        if (!kw->is_object()) { err = "chat_template_kwargs must be a JSON object"; return false; }
        if (!read_enable(*kw)) return false;
        for (const auto & item : kw->items()) merged[item.key()] = item.value().dump();
        if (kw->contains("reasoning_effort")) effort = &(*kw)["reasoning_effort"];
    }
    if (body.contains("reasoning_effort") && !body["reasoning_effort"].is_null()) effort = &body["reasoning_effort"];
    if (effort) {
        if (!effort->is_string() || effort->get_ref<const std::string &>().empty()) {
            err = "reasoning_effort must be a nonempty string";
            return false;
        }
        const auto & level = effort->get_ref<const std::string &>();
        if (level == "none") {
            enabled = false;
            merged.erase("reasoning_effort");
        } else if (level == "default") {
            merged.erase("reasoning_effort");
        } else {
            std::string normalized = level;
            if (!template_source.empty() && template_source.find("reasoning_effort") != std::string::npos &&
                !kvmem_chat_template_has_effort(template_source, level)) {
                normalized = kvmem_chat_template_default_effort(template_source);
                if (normalized.empty()) normalized = "default";
            }
            if (normalized == "default") merged.erase("reasoning_effort");
            else merged["reasoning_effort"] = json(normalized).dump();
        }
    }
    // Avoid a stale default kwarg overriding a request's explicit switch during rendering.
    merged["enable_thinking"] = json(enabled).dump();
    thinking = enabled;
    kwargs = std::move(merged);
    return true;
}
