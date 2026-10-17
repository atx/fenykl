/* Floating point (no FPU on MN1030/MN103S: soft-float via libgcc). */
float fadd(float a, float b) { return a + b; }
float fmul(float a, float b) { return a * b; }
float fdiv(float a, float b) { return a / b; }
double dadd(double a, double b) { return a + b; }
double dmul(double a, double b) { return a * b; }
double ddiv(double a, double b) { return a / b; }
int fcmp(float a, float b) { return a < b; }
int dcmp(double a, double b) { return a >= b; }
int f2i(float a) { return (int)a; }
unsigned d2u(double a) { return (unsigned)a; }
float i2f(int a) { return a; }
double u2d(unsigned a) { return a; }
double f2d(float a) { return a; }
float d2f(double a) { return a; }
long long d2ll(double a) { return (long long)a; }
