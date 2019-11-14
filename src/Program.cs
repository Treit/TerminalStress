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
                using (var fs = new FileStream(@"c:\temp\repro.txt", FileMode.Create))
                using (var sw = new StreamWriter(fs, Encoding.Unicode))
                {
                    char c = (char)r.Next(0xD100, 0xFA95);
                    s += c;

                    sw.WriteLine(s);
                    sw.Flush();
                    Console.WriteLine(s);
                }
            }
        }
    }
}