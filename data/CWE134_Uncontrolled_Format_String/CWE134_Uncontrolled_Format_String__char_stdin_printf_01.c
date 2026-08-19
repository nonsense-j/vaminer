/* Juliet-inspired synthetic example: CWE-134, flow variant 01. */

#include <stdio.h>

#define DATA_SIZE 100

static int read_input(char *data, size_t size)
{
    return fgets(data, (int)size, stdin) != NULL;
}

#ifndef OMITBAD
void CWE134_Uncontrolled_Format_String__char_stdin_printf_01_bad(void)
{
    char data[DATA_SIZE] = "";

    if (read_input(data, sizeof(data)))
    {
        /* FLAW: externally controlled text is interpreted as a format string. */
        printf(data);
    }
}
#endif

#ifndef OMITGOOD
static void goodB2G(void)
{
    char data[DATA_SIZE] = "";

    if (read_input(data, sizeof(data)))
    {
        /* FIX: the format string is constant; external text is a value. */
        printf("%s", data);
    }
}

void CWE134_Uncontrolled_Format_String__char_stdin_printf_01_good(void)
{
    goodB2G();
}
#endif

#ifdef INCLUDEMAIN
int main(void)
{
#ifndef OMITGOOD
    CWE134_Uncontrolled_Format_String__char_stdin_printf_01_good();
#endif
#ifndef OMITBAD
    CWE134_Uncontrolled_Format_String__char_stdin_printf_01_bad();
#endif
    return 0;
}
#endif
