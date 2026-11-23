/* Architecture-neutral inline hook facade.
 *
 * ARM32/ARM64 keep using the pinned ShadowHook; x86_64 has no ShadowHook
 * backend, so it uses Dobby. Callers must use only the four functions below.
 */
#pragma once

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    DUCK_HOOK_MODE_UNIQUE = 0,
} DuckHookMode;

int duck_hook_init(DuckHookMode mode, int allow_rehook);
void *duck_hook_sym_addr(void *address, void *replacement, void **original);
int duck_unhook(void *handle);
int duck_hook_errno(void);

#ifdef __cplusplus
}
#endif
