// Copyright (c) 2017-2023, University of Tennessee. All rights reserved.
// SPDX-License-Identifier: BSD-3-Clause
// This program is free software: you can redistribute it and/or modify it under
// the terms of the BSD 3-Clause license. See the accompanying LICENSE file.

#include <stdio.h>

#include "config.h"

#define LAPACK_ilaver FORTRAN_NAME( ilaver, ILAVER )

#ifdef ACCELERATE_NEW_LAPACK
    // A self-declared undecorated prototype would silently bind the legacy
    // LAPACK 3.2.1 symbol; Apple's header carries the $NEWLAPACK asm-label
    // decoration on ilaver_.
    #pragma message "include Accelerate.h"
    #include <stdlib.h>  // workaround
    #include <Accelerate/Accelerate.h>
#else
    #ifdef __cplusplus
    extern "C"
    #endif
    void LAPACK_ilaver( lapack_int* major, lapack_int* minor, lapack_int* patch );
#endif

int main( int argc, char** argv )
{
    using llong = long long;
    lapack_int major, minor, patch;
    LAPACK_ilaver( &major, &minor, &patch );
    printf( "LAPACK_VERSION=%lld.%02lld.%02lld\n",
            llong( major ), llong( minor ), llong( patch ) );
    return 0;
}
