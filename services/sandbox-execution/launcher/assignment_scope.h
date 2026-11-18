#ifndef BBX_ASSIGNMENT_SCOPE_H
#define BBX_ASSIGNMENT_SCOPE_H

int bbx_scope_prepare(void);
int bbx_scope_legacy_allowed(void);
int bbx_scope_read_context(void);
int bbx_scope_join(const char *key, const char *binding, const char *pod,
    const char *revision, const char *incarnation);
int bbx_scope_control(const char *mode, const char *key, const char *binding,
    const char *pod, const char *revision, const char *incarnation);

#endif
