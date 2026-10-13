/* Whether Hypervisor.framework creates a virtual machine here: prints the
 * status hv_vm_create returns, and exits 0 only if it succeeded. */
#include <Hypervisor/Hypervisor.h>
#include <stdio.h>

int main(void)
{
#if defined(__arm64__)
    hv_return_t r = hv_vm_create(NULL);
#else
    hv_return_t r = hv_vm_create(HV_VM_DEFAULT);
#endif
    printf("hv_vm_create: 0x%x\n", (unsigned)r);
    if (r == HV_SUCCESS)
        hv_vm_destroy();
    return r != HV_SUCCESS;
}
