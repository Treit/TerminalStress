namespace TerminalStress
{
    using System;
    using System.IO;
    using System.Text;

    class Program
    {
        static void Main(string[] args)
        {
            Random r = new Random();

            Console.OutputEncoding = Encoding.UTF8;

            string s = string.Empty;

            while (true)
            {
                char c = (char)r.Next(0xD100, 0xFA95);
                s += c;

                Console.WriteLine(s);

                if (s.Length > 100_000)
                {
                    s = string.Empty;
                }
            }
        }
    }
}