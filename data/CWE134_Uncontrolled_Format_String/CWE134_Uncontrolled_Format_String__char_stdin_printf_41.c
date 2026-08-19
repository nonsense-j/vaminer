/* Juliet-inspired synthetic example: CWE-134, flow variant 41. */

#include <stdio.h>

#define DATA_SIZE 100

static int read_input(char *data, size_t size)
{
    return fgets(data, (int)size, stdin) != NULL;
}

#ifndef OMITBAD
static void badSink(const char *data)
{
    /* FLAW: externally controlled text is interpreted as a format string. */
    printf(data);
}

void CWE134_Uncontrolled_Format_String__char_stdin_printf_41_bad(void)
{
    char data[DATA_SIZE] = "";

    if (read_input(data, sizeof(data)))
    {
        badSink(data);
    }
}
#endif

#ifndef OMITGOOD
static void goodB2GSink(const char *data)
{
    /* FIX: the format string is constant; external text is a value. */
    printf("%s", data);
}

static void goodB2G(void)
{
    char data[DATA_SIZE] = "";

    if (read_input(data, sizeof(data)))
    {
        goodB2GSink(data);
    }
}

void CWE134_Uncontrolled_Format_String__char_stdin_printf_41_good(void)
{
    goodB2G();
}
#endif

#ifdef INCLUDEMAIN
int main(void)
{
#ifndef OMITGOOD
    CWE134_Uncontrolled_Format_String__char_stdin_printf_41_good();
#endif
#ifndef OMITBAD
    CWE134_Uncontrolled_Format_String__char_stdin_printf_41_bad();
#endif
    return 0;
}
#endif
