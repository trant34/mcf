#include <exception>
#include <iostream>

#include "args.hpp"
#include "mf.hpp"

int main(int argc, char** argv) {
    try {
        mf::Args args = mf::parse_args(argc, argv);
        mf::MF mf_test(args);
        mf_test.run();
    } catch (const std::exception& exc) {
        std::cerr << "[MF][FATAL] " << exc.what() << std::endl;
        return 1;
    }
    return 0;
}
