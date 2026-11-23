/* Derived from Play Integrity Fork, osm0sis, chiteroman and contributors.
 * Source: https://github.com/osm0sis/PlayIntegrityFork/tree/23ec56c2440c4fd1a5e893a23ef7861d6366a828
 * SPDX-License-Identifier: GPL-3.0-only
 * Modified 2026-10-23: independent engine packaging, bounded configuration and runtime operations.
 */
#include "property_hook.hpp"
#include "duck_hook.h"
#include <android/log.h>
#include <atomic>
#include <dlfcn.h>
#define LOGD(...) __android_log_print(ANDROID_LOG_DEBUG, "PIF/Native", __VA_ARGS__)
namespace pif {
// The platform exposes one process-wide property callback. Its immutable owner is
// installed once before the hook is published and lives with the target process.
static std::atomic<PropertyHook *> installed{nullptr};
void PropertyHook::modify(void *opaque,const char *name,const char *value,uint32_t serial) {
    auto *context=static_cast<Context *>(opaque); auto *owner=context->owner;
    const char *replacement=value;
    if(name && value) {
        if (const auto *override = propertyOverride(owner->properties, name)) replacement=override->c_str();
        if(owner->logLevel>99 || replacement!=value) LOGD("[%s]: %s -> %s",name,value,replacement);
    }
    context->callback(context->cookie,name,replacement,serial);
}
void PropertyHook::read(const prop_info *info,Callback callback,void *cookie) {
    auto *owner=installed.load(std::memory_order_acquire);
    if(!owner || !owner->original) return;
    if(!info || !callback) return owner->original(info,callback,cookie);
    Context context{owner,callback,cookie};
    owner->original(info,modify,&context);
}
bool PropertyHook::install(const Settings &settings) {
    properties=settings.properties;logLevel=settings.verboseLogs;
    if(duck_hook_init(DUCK_HOOK_MODE_UNIQUE,false)!=0) { LOGD("inline hook initialization failed");return false; }
    void *symbol=dlsym(RTLD_DEFAULT,"__system_property_read_callback");
    if(!symbol) { LOGD("Property symbol unavailable");return false; }
    installed.store(this,std::memory_order_release);
    void *handle=duck_hook_sym_addr(symbol,reinterpret_cast<void *>(read),reinterpret_cast<void **>(&original));
    if(!handle) {installed.store(nullptr,std::memory_order_release);LOGD("Property hook failed: %d",duck_hook_errno());return false;}
    LOGD("Property hook initialized");return true;
}
}
