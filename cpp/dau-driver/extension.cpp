#include "dau-driver/extension.hpp"

#include <nanobind/nanobind.h>

namespace nb = nanobind;

namespace dau_driver {

const char* native_backend() { return "nanobind"; }

int native_abi_version() { return 1; }

} // namespace dau_driver

NB_MODULE(extension, module) {
  module.doc() = "DAU low-level native extension";
  module.def("native_backend", &dau_driver::native_backend);
  module.def("native_abi_version", &dau_driver::native_abi_version);
}
