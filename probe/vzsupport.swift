// What Virtualization.framework reports about running a virtual machine here.
import Virtualization

print("VZVirtualMachine.isSupported", VZVirtualMachine.isSupported)
if #available(macOS 15, *) {
    print("isNestedVirtualizationSupported", VZGenericPlatformConfiguration.isNestedVirtualizationSupported)
}
