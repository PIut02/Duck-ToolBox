/* See duck_hook.h. The ARM path is a thin rename of the pinned ShadowHook API. */
#include "duck_hook.h"

#if defined(__x86_64__)
#include <dobby.h>

int duck_hook_init(DuckHookMode mode, int allow_rehook) {
    (void)mode;
    (void)allow_rehook;
    // Dobby installs hooks lazily and has no global init step.
    return 0;
}

void *duck_hook_sym_addr(void *address, void *replacement, void **original) {
    if (!address || !replacement) return nullptr;
    return DobbyHook(address, replacement, reinterpret_cast<void **>(original)) == 0
               ? address
               : nullptr;
}

int duck_unhook(void *handle) {
    return handle ? DobbyDestroy(handle) : -1;
}

int duck_hook_errno(void) {
    return 0;  // Dobby reports failures through hook return values.
}

#else
#include "shadowhook.h"

extern "C" int sh_linker_init(void) { return 0; }

int duck_hook_init(DuckHookMode mode, int allow_rehook) {
    return shadowhook_init(static_cast<shadowhook_mode_t>(mode), allow_rehook != 0);
}

void *duck_hook_sym_addr(void *address, void *replacement, void **original) {
    return shadowhook_hook_sym_addr(address, replacement, original);
}

int duck_unhook(void *handle) {
    return shadowhook_unhook(handle);
}

int duck_hook_errno(void) {
    return shadowhook_get_errno();
}
#endif
